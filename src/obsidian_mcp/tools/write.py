"""Mutating tools.

Every function here snapshots to the journal *before* touching the vault. That
ordering is the whole safety argument for exposing these on a public endpoint:
if a write turns out to be wrong -- or malicious -- the previous bytes are
already saved somewhere no MCP tool can reach.

Tool names match the Obsidian connector used on the Mac (vault_read/vault_write/
vault_patch/...), so prompts and habits carry across the two servers even though
one talks to a running Obsidian and this one reads the disk.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..errors import StaleWrite, ToolError
from ..links import LinkResolver, parse_links
from ..notes import (
    PatchTargetMissing,
    append_to_body,
    compose,
    parse,
    patch as patch_body,
    rewrite_link_targets,
)
from ..telemetry import instrumented
from ._common import DESTRUCTIVE, WRITES, Context

log = logging.getLogger(__name__)

# Slack for clients that round mtime_ns through a double. Measured error when
# re-serialising 1788422843329066594 at these magnitudes: 98 ns at 17
# significant digits, 610 ns at 16, 3486 ns at 15. Standard serialisers emit
# shortest-round-trip form (17), but 10us covers a truncating one too and is
# still 100x below the millisecond granularity of a real edit -- so a genuine
# concurrent write is caught with plenty of margin either way.
MTIME_TOLERANCE_NS = 10_000



def _parse_mtime(value: str | int) -> int:
    """Coerce a client-supplied mtime to nanoseconds.

    Accepts scientific notation and trailing `.0` as well as a plain integer,
    because those are exactly what a client that parsed the value as a JSON
    number emits when it serialises it back -- `1.788422843329067e+18` rather
    than the digits. Rejecting those would defeat the point of the tolerance
    below, which exists for precisely those clients.
    """
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        raise ToolError(
            f"expected_mtime_ns={value!r} is not an integer nanosecond "
            "timestamp. Pass the mtime_ns string from vault_read back "
            "unchanged; it is a string because the value is too large for a "
            "JSON number to carry exactly."
        ) from None


def register(mcp: MCPServer, ctx: Context) -> None:
    """Attach every mutating tool."""

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_append", mutation=True)
    async def vault_append(
        path: str,
        content: str,
        vault: str | None = None,
        heading: str | None = None,
        create_if_missing: bool = True,
        ensure_blank_line: bool = True,
    ) -> dict[str, Any]:
        """Append text to the end of a note, or to the end of one section.

        With `heading`, the text lands at the end of that section -- after its
        existing content, before the next heading of the same or higher level --
        which is what "add this under ## Log" means. If the heading is absent it
        is created at the end of the note rather than failing, because silently
        losing a capture from a phone is worse than a slightly wrong location.

        This is the primary capture tool. Frontmatter is preserved. For anything
        more precise than "the end of", use vault_patch.
        """
        target = ctx.vault(vault)
        if not content.strip():
            raise ToolError("Nothing to append: `content` is empty.")

        resolved = target.resolve(path, must_exist=False)
        created = not resolved.exists()

        if created and not create_if_missing:
            raise ToolError(
                f"No note at {path!r} and create_if_missing is False. Use "
                "vault_write, or set create_if_missing=True."
            )

        version_id = ctx.journal.snapshot(target, resolved, op="append")
        existing = "" if created else ctx.read(target, resolved)
        updated = append_to_body(
            existing, content, heading=heading, ensure_blank_line=ensure_blank_line
        )
        target.write_text_atomic(resolved, updated)

        rel = target.relative(resolved)
        ctx.journal.record(
            tool="vault_append",
            vault=target.name,
            path=rel,
            version_id=version_id,
            # Not `created`: that is a reserved logging.LogRecord field, and
            # journal.record() forwards these straight into a log record.
            note_created=created,
            bytes_added=len(content),
        )
        return {
            "vault": target.name,
            "path": rel,
            "created": created,
            "heading": heading,
            "bytes_written": len(updated),
            "previous_version_id": version_id,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_write", mutation=True)
    async def vault_write(
        path: str,
        content: str = "",
        vault: str | None = None,
        frontmatter: dict[str, Any] | None = None,
        mode: str = "create",
        expected_mtime_ns: str | int | None = None,
    ) -> dict[str, Any]:
        """Write a note, creating it or replacing its contents.

        `mode` is "create" (the default -- refuses if the note already exists) or
        "overwrite" (replaces it). Choosing the default deliberately: a write
        that silently destroys an existing note is the mistake this guards.

        `content` is the body below the frontmatter. Pass `frontmatter` to set
        it; leave it None on an overwrite to keep the note's existing
        frontmatter.

        Pass `expected_mtime_ns` from a preceding vault_read to make an
        overwrite fail rather than clobber a change Dropbox delivered from
        another device in between. Strongly recommended whenever the new content
        was derived from what the note already said.

        The previous contents are snapshotted first -- recover with
        list_versions and restore_version.
        """
        if mode not in {"create", "overwrite"}:
            raise ToolError(f"mode must be 'create' or 'overwrite'; got {mode!r}.")

        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=False)
        exists = resolved.exists()

        if exists and mode == "create":
            raise ToolError(
                f"A note already exists at {path!r}. Use vault_append to add to "
                "it, vault_patch to change part of it, or pass mode='overwrite' "
                "if replacing it entirely is really what you want."
            )
        if not exists and mode == "overwrite" and expected_mtime_ns is not None:
            raise ToolError(
                f"No note at {path!r}, but expected_mtime_ns was given. It may "
                "have been deleted since you read it -- check with vault_read."
            )

        if exists and expected_mtime_ns is not None:
            actual = resolved.stat().st_mtime_ns
            expected = _parse_mtime(expected_mtime_ns)
            # Tolerance, not equality. A client that parsed the value as a JSON
            # number has already rounded it -- doubles cannot represent 1.8e18
            # exactly, and the error is a few hundred nanoseconds. Real edits
            # move mtime by milliseconds at least, so this cannot mask one.
            if abs(actual - expected) > MTIME_TOLERANCE_NS:
                raise StaleWrite(
                    f"{path!r} changed since you read it, so the write was not "
                    "applied -- applying it would have discarded that change. "
                    "Call vault_read again, rebase your content on the current "
                    "note, and retry."
                )

        metadata = frontmatter
        if exists and frontmatter is None:
            metadata = parse(ctx.read(target, resolved)).metadata

        version_id = ctx.journal.snapshot(target, resolved, op="write")
        target.write_text_atomic(resolved, compose(metadata, content))

        rel = target.relative(resolved)
        ctx.journal.record(
            tool="vault_write",
            vault=target.name,
            path=rel,
            version_id=version_id,
            mode=mode,
            overwrote=version_id is not None,
        )
        return {
            "vault": target.name,
            "path": rel,
            "created": not exists,
            "overwrote": version_id is not None,
            "mtime_ns": str(resolved.stat().st_mtime_ns),
            "previous_version_id": version_id,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_patch", mutation=True)
    async def vault_patch(
        path: str,
        content: str,
        target_type: str = "heading",
        target: str = "",
        operation: str = "append",
        vault: str | None = None,
    ) -> dict[str, Any]:
        """Insert or replace content at a specific place in a note.

        The precise counterpart to vault_append, for when "the end of the file"
        is not where the text belongs.

        - `target_type="heading"`, `target="Log"` -- or `"Parent::Child"` for a
          nested heading. operation "append" puts the text at the end of that
          section, "prepend" just under the heading, "replace" swaps the
          section's body while keeping the heading itself.
        - `target_type="block"`, `target="^block-id"` -- relative to the block
          carrying that reference. A "replace" keeps the ^id so links to it
          survive.
        - `target_type="frontmatter"`, `target="status"` -- sets, appends to, or
          prepends to a frontmatter property.

        Unlike vault_append, a missing target is an error rather than being
        created: a patch names a specific place, so failing to find it means the
        caller's assumption about the note was wrong.
        """
        vault_obj = ctx.vault(vault)
        if not target.strip():
            raise ToolError(
                "`target` is required: the heading, ^block-id, or frontmatter "
                "key to patch relative to."
            )

        resolved = vault_obj.resolve(path, must_exist=True)
        raw = ctx.read(vault_obj, resolved)

        try:
            updated = patch_body(
                raw,
                operation=operation,
                target_type=target_type,
                target=target,
                content=content,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        except PatchTargetMissing:
            hint = {
                "heading": (
                    "Call vault_read or vault_get_document_map to see the note's "
                    "actual headings. Use '::' for a nested heading path."
                ),
                "block": "Block references look like '^my-id' at the end of a line.",
                "frontmatter": "Call vault_read to see the note's frontmatter keys.",
            }.get(target_type, "")
            raise ToolError(
                f"No {target_type} matching {target!r} in {path!r}, so nothing "
                f"was changed. {hint}"
            ) from None

        version_id = ctx.journal.snapshot(vault_obj, resolved, op="patch")
        vault_obj.write_text_atomic(resolved, updated)

        rel = vault_obj.relative(resolved)
        ctx.journal.record(
            tool="vault_patch",
            vault=vault_obj.name,
            path=rel,
            version_id=version_id,
            operation=operation,
            target_type=target_type,
            target=target,
        )
        return {
            "vault": vault_obj.name,
            "path": rel,
            "operation": operation,
            "target_type": target_type,
            "target": target,
            "previous_version_id": version_id,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_move", mutation=True)
    async def vault_move(
        path: str,
        destination: str,
        vault: str | None = None,
        update_links: bool = True,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Move or rename a note, repointing the links that referenced it.

        With update_links=True (the default) every `[[wikilink]]` that resolved
        to this note is rewritten to the new name, the way Obsidian does on
        rename. Without it, a move silently breaks every backlink -- which is why
        it is not the default.

        Aliases, subpaths, and embeds on those links are preserved; only the
        target changes.
        """
        target_vault = ctx.vault(vault)
        source = target_vault.resolve(path, must_exist=True)
        dest = target_vault.resolve(destination, must_exist=False)

        if source == dest:
            raise ToolError("Source and destination are the same note.")
        if dest.exists() and not overwrite:
            raise ToolError(
                f"A note already exists at {destination!r}. Pass overwrite=True "
                "to replace it."
            )

        old_rel = target_vault.relative(source)
        new_rel = target_vault.relative(dest)
        content = ctx.read(target_vault, source)

        # Work out which links point here BEFORE moving. LinkResolver indexes
        # the vault as it currently stands, so once the source is unlinked
        # nothing can resolve to it any more and the only thing left would be
        # matching link text -- which rewrites same-named notes elsewhere that
        # never pointed here at all.
        plan = _plan_relink(ctx, target_vault, old_rel) if update_links else {}

        source_version = ctx.journal.snapshot(target_vault, source, op="move")
        dest_version = ctx.journal.snapshot(target_vault, dest, op="move-overwrite")

        target_vault.write_text_atomic(dest, content)
        source.unlink()

        updated_notes = _apply_relink(ctx, target_vault, plan, new_rel) if plan else []

        ctx.journal.record(
            tool="vault_move",
            vault=target_vault.name,
            path=old_rel,
            destination=new_rel,
            version_id=source_version,
            overwrote_version_id=dest_version,
            links_updated_in=updated_notes,
            links_updated_count=len(updated_notes),
        )
        return {
            "vault": target_vault.name,
            "path": new_rel,
            "moved_from": old_rel,
            "links_updated_in": updated_notes,
            "previous_version_id": source_version,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_copy", mutation=True)
    async def vault_copy(
        path: str,
        destination: str,
        vault: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Copy a note to a new path.

        Links inside the copy are left exactly as they were -- a duplicate of a
        note should still point where the original pointed.
        """
        target_vault = ctx.vault(vault)
        source = target_vault.resolve(path, must_exist=True)
        dest = target_vault.resolve(destination, must_exist=False)

        if source == dest:
            raise ToolError("Source and destination are the same note.")
        if dest.exists() and not overwrite:
            raise ToolError(
                f"A note already exists at {destination!r}. Pass overwrite=True "
                "to replace it."
            )

        content = ctx.read(target_vault, source)
        version_id = ctx.journal.snapshot(target_vault, dest, op="copy-overwrite")
        target_vault.write_text_atomic(dest, content)

        rel = target_vault.relative(dest)
        ctx.journal.record(
            tool="vault_copy",
            vault=target_vault.name,
            path=rel,
            copied_from=target_vault.relative(source),
            version_id=version_id,
        )
        return {
            "vault": target_vault.name,
            "path": rel,
            "copied_from": target_vault.relative(source),
            "overwrote": version_id is not None,
            "previous_version_id": version_id,
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    @instrumented("vault_delete", mutation=True)
    async def vault_delete(
        path: str, vault: str | None = None, to_trash: bool = True
    ) -> dict[str, Any]:
        """Delete a note.

        By default the note is moved into the vault's `.trash/` folder --
        Obsidian's own convention -- rather than unlinked, so it is recoverable
        from the vault itself and Dropbox replicates the move. A snapshot is also
        written to the version journal first, so the contents survive even with
        to_trash=False.

        Links pointing at the deleted note are left alone; call list_backlinks
        first if you need to know what will break.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        rel = target.relative(resolved)

        version_id = ctx.journal.snapshot(target, resolved, op="delete")
        content = ctx.read(target, resolved)

        trashed_to = None
        if to_trash:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            # Timestamp the name so deleting two same-named notes from different
            # folders does not silently overwrite the first one in the trash.
            trash_path = (
                target.root / ".trash" / f"{resolved.stem}-{stamp}{resolved.suffix}"
            )
            trash_path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-unlink rather than rename: the destination is inside
            # `.trash/`, which vault.resolve() refuses, so this stays on the
            # primitives vault.py exposes instead of reaching around them.
            target.write_text_atomic(trash_path, content)
            trashed_to = f".trash/{trash_path.name}"

        resolved.unlink()

        ctx.journal.record(
            tool="vault_delete",
            vault=target.name,
            path=rel,
            version_id=version_id,
            trashed_to=trashed_to,
        )
        return {
            "vault": target.name,
            "path": rel,
            "trashed_to": trashed_to,
            "previous_version_id": version_id,
            "recover_with": "restore_version",
        }


def _plan_relink(ctx: Context, vault, old_rel: str) -> dict[str, set[str]]:
    """Map note path -> the link spellings in it that resolve to ``old_rel``.

    Must run while ``old_rel`` still exists: resolution is what distinguishes a
    link that genuinely points at this note from a same-named note somewhere
    else in the vault. Matching on link text alone cannot tell them apart, and
    rewriting on that basis silently repoints links that were never ours.
    """
    resolver = LinkResolver(vault)
    plan: dict[str, set[str]] = {}
    for path in vault.iter_notes():
        rel = vault.relative(path)
        if rel == old_rel:
            continue
        try:
            note = parse(ctx.read(vault, path))
        except (OSError, ToolError):
            continue
        forms = {
            ref.target
            for ref in parse_links(note.body)
            if resolver.resolve(ref.target, source=rel)[0] == old_rel
        }
        if forms:
            plan[rel] = forms
    return plan


def _apply_relink(
    ctx: Context, vault, plan: dict[str, set[str]], new_rel: str
) -> list[str]:
    """Rewrite the planned link spellings to point at ``new_rel``.

    Chooses the shortest form that still resolves unambiguously -- the bare
    basename when it is unique after the move, the full path otherwise. That is
    what Obsidian writes, and it keeps diffs small.
    """
    from pathlib import Path as _Path

    resolver = LinkResolver(vault)
    new_stem = _Path(new_rel).stem
    same_stem = [
        rel for rel in resolver.all_paths if _Path(rel).stem.lower() == new_stem.lower()
    ]
    replacement = new_stem if len(same_stem) <= 1 else new_rel.removesuffix(".md")

    touched: list[str] = []
    for rel, forms in plan.items():
        path = vault.root / rel
        try:
            note = parse(ctx.read(vault, path))
        except (OSError, ToolError):
            continue
        body, total = note.body, 0
        for form in forms:
            body, count = rewrite_link_targets(body, form, replacement)
            total += count
        if not total:
            continue
        ctx.journal.snapshot(vault, path, op="relink")
        vault.write_text_atomic(
            path, compose(note.metadata, body) if note.metadata else body
        )
        touched.append(rel)
    return touched
