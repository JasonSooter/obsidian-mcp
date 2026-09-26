"""Read-only tools. None of these mutate the vault or touch the network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import links as links_mod
from .. import search as search_mod
from ..errors import ToolError
from .. import notes as notes_mod
from ..notes import parse
from ..telemetry import instrumented
from ._common import (
    MAX_LIST_LIMIT,
    MAX_SEARCH_LIMIT,
    READ_ONLY,
    Context,
    clamp,
    iso,
    load_note,
    matches_filters,
    summarize,
)


def register(mcp: MCPServer, ctx: Context) -> None:
    """Attach the read-only tools."""

    default_vault = ctx.vaults.default_name

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("search_query")
    async def search_query(
        query: str = "",
        vault: str | None = None,
        tag: str | None = None,
        properties: dict[str, Any] | None = None,
        folder: str | None = None,
        case_sensitive: bool = False,
        fixed_string: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Full-text search across the vault, with tag and frontmatter filters.

        `query` is a regular expression by default; set fixed_string=True to
        search for it literally. Leave `query` empty to filter purely by `tag`
        or `properties` -- e.g. every note with `status: active`.

        `properties` matches frontmatter keys, ANDed together and with the
        query. A list-valued property matches if any element matches.

        Every call reads the vault fresh from disk, so results always reflect
        what Dropbox has most recently delivered.
        """
        target = ctx.vault(vault)
        limit = clamp(limit, MAX_SEARCH_LIMIT)
        root = target.resolve_dir(folder)

        if not query.strip() and not tag and not properties:
            raise ToolError(
                "Give something to search for: a query, a tag, or a properties "
                "filter. To browse without a query, use vault_list."
            )

        results: list[dict[str, Any]] = []

        if query.strip():
            # Over-fetch, because tag/property filters are applied after the
            # text match and would otherwise thin the page below `limit`.
            hits = search_mod.run(
                target,
                query,
                root=root,
                case_sensitive=case_sensitive,
                fixed_string=fixed_string,
                max_results=limit * 5,
            )
            seen: set[Path] = set()
            for hit in hits:
                if len(results) >= limit:
                    break
                if hit.path in seen:
                    continue
                seen.add(hit.path)
                try:
                    note = load_note(ctx, target, hit.path)
                except (OSError, ToolError):
                    continue
                if not matches_filters(note, tag=tag, properties=properties):
                    continue
                entry = summarize(target, hit.path, note)
                entry["line_number"] = hit.line_number
                entry["excerpt"] = search_mod.excerpt(
                    hit.line_text, query, fixed_string=fixed_string
                )
                results.append(entry)
        else:
            for path in search_mod.list_all_paths(target, root):
                if len(results) >= limit:
                    break
                try:
                    note = load_note(ctx, target, path)
                except (OSError, ToolError):
                    continue
                if not matches_filters(note, tag=tag, properties=properties):
                    continue
                results.append(summarize(target, path, note))
            results.sort(key=lambda entry: entry["modified"], reverse=True)

        return {
            "vault": target.name,
            "count": len(results),
            "truncated": len(results) >= limit,
            "results": results,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("vault_list")
    async def vault_list(
        vault: str | None = None,
        folder: str | None = None,
        sort: str = "modified",
        recursive: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List notes in the vault, most recently modified first by default.

        `sort` is one of "modified", "created", or "path". Use folder=None for
        the whole vault. This is the tool for "what have I been working on".
        """
        target = ctx.vault(vault)
        limit = clamp(limit, MAX_LIST_LIMIT)
        root = target.resolve_dir(folder)

        if sort not in {"modified", "created", "path"}:
            raise ToolError(
                f"sort={sort!r} is not valid. Use 'modified', 'created', or 'path'."
            )

        paths = (
            list(target.iter_notes(root))
            if recursive
            else [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".md"]
        )

        def key(path: Path):
            stat = path.stat()
            if sort == "path":
                return target.relative(path)
            # st_birthtime is macOS-only; Linux has no creation time, so
            # "created" degrades to ctime rather than failing on Linux.
            if sort == "created":
                return -getattr(stat, "st_birthtime", stat.st_ctime)
            return -stat.st_mtime

        try:
            paths.sort(key=key)
        except OSError:
            # A file vanished mid-sort (Dropbox deleted it). Re-filter and retry.
            paths = [p for p in paths if p.exists()]
            paths.sort(key=key)

        listed = []
        for path in paths[:limit]:
            try:
                listed.append(summarize(target, path))
            except OSError:
                continue

        return {
            "vault": target.name,
            "folder": folder or "/",
            "count": len(listed),
            "total": len(paths),
            "truncated": len(paths) > limit,
            "notes": listed,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("vault_read")
    async def vault_read(path: str, vault: str | None = None) -> dict[str, Any]:
        """Read one note: frontmatter, body, tags, and outgoing links.

        The returned `mtime_ns` is a STRING; pass it back to vault_write's
        `expected_mtime_ns` verbatim. It is a string because the value is too
        large for a JSON number to carry exactly.
        Passing it back makes the write fail rather than silently overwrite if
        Dropbox delivered a change from another device in between.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        raw = ctx.read(target, resolved)
        note = parse(raw)
        stat = resolved.stat()

        return {
            "vault": target.name,
            "path": target.relative(resolved),
            "title": note.title,
            "frontmatter": note.metadata,
            "body": note.body,
            "tags": note.tags,
            "links": [ref.target for ref in links_mod.parse_links(note.body)],
            "modified": iso(stat.st_mtime),
            # A STRING, deliberately. Nanosecond mtimes are ~1.8e18, which is
            # 199x above the largest integer a double can hold exactly, so any
            # client parsing JSON numbers as doubles (most of them) rounds it
            # and sends back a value tens of nanoseconds off. That made
            # expected_mtime_ns fail every time -- the guard was unusable from
            # a real client, and the obvious workaround is to drop it, which
            # removes the protection entirely.
            "mtime_ns": str(stat.st_mtime_ns),
            "size": stat.st_size,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("resolve_links")
    async def resolve_links(path: str, vault: str | None = None) -> dict[str, Any]:
        """Resolve every [[wikilink]] in a note to a real vault path.

        Uses Obsidian's shortest-unique-path rules, not plain filename matching:
        an exact path wins, then a path relative to this note's folder, then a
        unique basename. When a basename is ambiguous the best guess is returned
        along with `ambiguous_candidates` listing the alternatives -- check that
        field before treating a result as certain.

        `exists: false` means the link is broken (or points at a non-markdown
        attachment, which this server does not index).
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        note = parse(ctx.read(target, resolved))
        rel = target.relative(resolved)
        results = links_mod.outgoing(target, rel, note.body)

        return {
            "vault": target.name,
            "path": rel,
            "count": len(results),
            "broken": [r["target"] for r in results if not r["exists"]],
            "links": results,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("list_backlinks")
    async def list_backlinks(
        path: str, vault: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Find every note that links to this one.

        Resolves each candidate link the way Obsidian would, so `[[Note]]`,
        `[[folder/Note]]`, and `[[Note|alias]]` all count as backlinks to the
        same note, while a same-named note in another folder does not.

        This scans the whole vault on every call -- correct under concurrent
        Dropbox writes, but the slowest tool here on a very large vault.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=True)
        rel = target.relative(resolved)
        limit = clamp(limit, MAX_LIST_LIMIT)

        resolver = links_mod.LinkResolver(target)
        found: list[dict[str, Any]] = []

        # Cheap prefilter: only files containing '[[' can possibly link here.
        candidates = search_mod.run(
            target,
            r"\[\[",
            root=target.root,
            case_sensitive=False,
            fixed_string=False,
            max_results=10_000,
        )
        for hit in {c.path for c in candidates}:
            if len(found) >= limit:
                break
            if hit == resolved:
                continue
            try:
                note = parse(ctx.read(target, hit))
            except (OSError, ToolError):
                continue
            source_rel = target.relative(hit)
            for ref in links_mod.parse_links(note.body):
                hit_path, _ = resolver.resolve(ref.target, source=source_rel)
                if hit_path == rel:
                    found.append(
                        {
                            "source_path": source_rel,
                            "line_number": ref.line_number,
                            "context": ref.line_text[:300],
                            "alias": ref.alias,
                            "is_embed": ref.is_embed,
                        }
                    )
                    break

        return {
            "vault": target.name,
            "path": rel,
            "count": len(found),
            "truncated": len(found) >= limit,
            "backlinks": found,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("search_simple")
    async def search_simple(
        query: str,
        vault: str | None = None,
        context_length: int = 200,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Plain-text search across the vault.

        The query is matched literally -- no regex, no filters. Use this when you
        just want to find a phrase; use search_query when you need tag or
        frontmatter filters or regular expressions.
        """
        target = ctx.vault(vault)
        limit = clamp(limit, MAX_SEARCH_LIMIT)
        if not query.strip():
            raise ToolError("`query` is empty. Give some text to search for.")

        hits = search_mod.run(
            target,
            query,
            root=target.root,
            case_sensitive=False,
            fixed_string=True,
            max_results=limit,
        )
        results = []
        for hit in hits:
            try:
                entry = summarize(target, hit.path)
            except OSError:
                continue
            entry["line_number"] = hit.line_number
            # search_simple always passes the query to ripgrep literally.
            entry["excerpt"] = search_mod.excerpt(
                hit.line_text, query, width=max(40, context_length), fixed_string=True
            )
            results.append(entry)

        return {
            "vault": target.name,
            "query": query,
            "count": len(results),
            "truncated": len(results) >= limit,
            "results": results,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("tag_list")
    async def tag_list(
        vault: str | None = None, folder: str | None = None
    ) -> dict[str, Any]:
        """List every tag in the vault with how many notes use it.

        Covers both frontmatter tags and inline #tags. Use it to discover the
        exact spelling of a tag before filtering on it with search_query.
        """
        target = ctx.vault(vault)
        root = target.resolve_dir(folder)

        counts: dict[str, int] = {}
        canonical: dict[str, str] = {}
        for path in target.iter_notes(root):
            try:
                note = load_note(ctx, target, path)
            except (OSError, ToolError):
                continue
            for tag in set(t.lower() for t in note.tags):
                counts[tag] = counts.get(tag, 0) + 1
            for tag in note.tags:
                canonical.setdefault(tag.lower(), tag)

        tags = [
            {"tag": canonical[key], "count": value}
            for key, value in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {"vault": target.name, "count": len(tags), "tags": tags}

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("vault_get_document_map")
    async def vault_get_document_map(
        vault: str | None = None,
        folder: str | None = None,
        include_headings: bool = True,
        limit: int = 500,
    ) -> dict[str, Any]:
        """An outline of the whole vault: paths, titles, tags, and headings.

        The orientation tool -- call it to see how the vault is organised before
        searching or writing, rather than guessing at paths. Bodies are not
        included, so this stays cheap on a large vault; set include_headings=False
        to make it cheaper still.
        """
        target = ctx.vault(vault)
        root = target.resolve_dir(folder)
        limit = clamp(limit, MAX_LIST_LIMIT)

        documents = []
        folders: set[str] = set()
        paths = sorted(target.iter_notes(root), key=lambda p: target.relative(p))
        for path in paths[:limit]:
            try:
                note = load_note(ctx, target, path)
                stat = path.stat()
            except (OSError, ToolError):
                continue
            rel = target.relative(path)
            parent = str(Path(rel).parent)
            if parent != ".":
                folders.add(parent)
            entry: dict[str, Any] = {
                "path": rel,
                "title": note.title,
                "tags": note.tags,
                "modified": iso(stat.st_mtime),
                "link_count": len(links_mod.parse_links(note.body)),
            }
            if include_headings:
                entry["headings"] = notes_mod.headings(note.body)
            documents.append(entry)

        return {
            "vault": target.name,
            "folder": folder or "/",
            "folders": sorted(folders),
            "count": len(documents),
            "total": len(paths),
            "truncated": len(paths) > limit,
            "documents": documents,
        }
