"""Quartz publishing tools.

`publish_site` with dry_run=False is the only action in this server that cannot
be undone, because content that has reached a public website has reached it. So:
dry_run defaults to True, the build must pass before anything is committed, and
every real run lands in the audit log with its commit sha.

Registered only when QUARTZ_ENABLED is true. When it is false these tools are
never constructed, so a direct tools/call by name gets the SDK's unknown-tool
error -- the surface is genuinely absent, not merely hidden.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..errors import ToolError
from ..notes import parse, set_frontmatter_key
from ..quartz import PUBLISH_KEY, is_published
from ..telemetry import instrumented
from ._common import READ_ONLY, WRITES, Context, summarize

log = logging.getLogger(__name__)


def register(mcp: MCPServer, ctx: Context) -> None:
    publisher = ctx.publisher
    if publisher is None:  # pragma: no cover -- guarded by the caller
        raise RuntimeError("publish tools registered without a publisher")

    def _collect_published(vault) -> dict[str, tuple[str, float]]:
        """Every note flagged `publish: true`, read fresh, with its mtime.

        The mtime travels with the contents because it becomes the `modified`
        and `published` frontmatter timestamps the site renders -- see
        quartz.render_for_publish.
        """
        found: dict[str, tuple[str, float]] = {}
        for path in vault.iter_notes():
            try:
                raw = ctx.read(vault, path)
                mtime = path.stat().st_mtime
            except (OSError, ToolError):
                continue
            if is_published(raw):
                found[vault.relative(path)] = (raw, mtime)
        return found

    @mcp.tool(annotations=WRITES)
    @instrumented("set_publish_status", mutation=True)
    async def set_publish_status(
        path: str, publish: bool, vault: str | None = None
    ) -> dict[str, Any]:
        """Flag or unflag a note for the public Quartz site.

        Sets `publish: true` in the note's frontmatter, which is what Quartz's
        ExplicitPublish filter reads. Setting it to False removes the key, which
        takes the note off the site on the next publish_site.

        This only changes the flag -- nothing is published until publish_site is
        called with dry_run=False.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        raw = ctx.read(target, resolved)

        version_id = ctx.journal.snapshot(target, resolved, op="publishflag")
        updated = set_frontmatter_key(raw, PUBLISH_KEY, True if publish else None)
        target.write_text_atomic(resolved, updated)

        rel = target.relative(resolved)
        ctx.journal.record(
            tool="set_publish_status",
            vault=target.name,
            path=rel,
            version_id=version_id,
            publish=publish,
        )
        return {
            "vault": target.name,
            "path": rel,
            "publish": publish,
            "note": "Flag changed only. Call publish_site to update the site.",
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("list_published")
    async def list_published(vault: str | None = None) -> dict[str, Any]:
        """List every note currently flagged `publish: true`.

        This is the "what would go live" answer. Worth calling before
        publish_site, especially before the first one.
        """
        target = ctx.vault(vault)
        entries = []
        for rel, (raw, _mtime) in sorted(_collect_published(target).items()):
            note = parse(raw)
            path = target.root / rel
            entry = summarize(target, path, note)
            entry["slug"] = note.metadata.get("slug") or rel.removesuffix(".md")
            entries.append(entry)
        return {"vault": target.name, "count": len(entries), "published": entries}

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("preview_publish")
    async def preview_publish(path: str, vault: str | None = None) -> dict[str, Any]:
        """Show exactly what Quartz would publish for one note, without building.

        Cheap check for private frontmatter or unresolved links before a note
        goes public. Warns if the note is not actually flagged.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        raw = ctx.read(target, resolved)
        note = parse(raw)
        rel = target.relative(resolved)

        warnings = []
        if not is_published(raw):
            warnings.append(
                "This note is not flagged `publish: true`, so it would NOT be "
                "included. Call set_publish_status first."
            )
        # Frontmatter keys that commonly hold things not meant to be public.
        for key in note.metadata:
            if key.lower() in {"private", "secret", "token", "password", "api_key"}:
                warnings.append(f"Frontmatter key {key!r} would be published as-is.")

        return {
            "vault": target.name,
            "path": rel,
            "would_publish": is_published(raw),
            "frontmatter": note.metadata,
            "rendered_body": note.body,
            "warnings": warnings,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("publish_site", mutation=True)
    async def publish_site(
        vault: str | None = None,
        dry_run: bool = True,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Publish every flagged note to the public Quartz site.

        Stages the `publish: true` notes into the site repo, runs the Quartz
        build as a gate, then commits and pushes.

        **dry_run defaults to True**: the first call stages and builds but
        pushes nothing, returning the exact set of additions, updates, and
        removals. Review that, then call again with dry_run=False to publish.

        This is the only irreversible action available here -- once content is
        on a public website it cannot be recalled. Removals happen too: a note
        that is no longer flagged is taken off the site.
        """
        target = ctx.vault(vault)

        with publisher.lock():
            published = _collect_published(target)
            plan = publisher.stage(target, published)

            if plan.is_empty:
                publisher.revert_staging()
                return {
                    "vault": target.name,
                    "dry_run": dry_run,
                    "changes": plan.as_dict(),
                    "built": False,
                    "pushed": False,
                    "detail": "The site already matches the flagged notes.",
                }

            try:
                publisher.build()
            except ToolError:
                # Leave the repo exactly as we found it -- a failed build must
                # not leave half-staged content for the next call to commit.
                publisher.revert_staging()
                raise

            if dry_run:
                publisher.revert_staging()
                return {
                    "vault": target.name,
                    "dry_run": True,
                    "changes": plan.as_dict(),
                    "built": True,
                    "pushed": False,
                    "detail": (
                        f"{plan.as_dict()['total_changes']} change(s) staged and "
                        "the build passed. Nothing was pushed. Call again with "
                        "dry_run=False to publish."
                    ),
                }

            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            commit_message = message or f"Publish {len(published)} note(s) — {stamp}"
            sha = publisher.commit_and_push(commit_message)

            ctx.journal.record(
                tool="publish_site",
                vault=target.name,
                path="<site>",
                commit_sha=sha,
                added=sorted(plan.added),
                updated=sorted(plan.updated),
                removed=sorted(plan.removed),
                # Scalar counts as well as the path lists: telemetry ships only
                # scalars (a list would flatten into an unqueryable string), and
                # the Grafana panels count publishes by these.
                added_count=len(plan.added),
                updated_count=len(plan.updated),
                removed_count=len(plan.removed),
            )
            log.info("published %s to %s", sha, publisher.remote)

            return {
                "vault": target.name,
                "dry_run": False,
                "changes": plan.as_dict(),
                "built": True,
                "pushed": True,
                "commit_sha": sha,
            }
