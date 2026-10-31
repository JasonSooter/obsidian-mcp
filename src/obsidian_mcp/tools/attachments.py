"""Binary attachment tools: write, read and list images in the vault.

The text tools refuse anything but notes. These are their binary counterpart,
deliberately narrower: image types only, only under the configured attachment
folders (default `Attachments/`), size-capped, and content-checked so the bytes
must be what the extension claims -- see attachments.py.

Writes follow the same safety rules as notes: atomic replace (Dropbox never
syncs half a file), journal snapshot before an overwrite (list_versions and
restore_version undo it), and an audit log entry.
"""

from __future__ import annotations

import base64
from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import attachments as att
from ..errors import ToolError
from ..telemetry import instrumented
from ._common import MAX_LIST_LIMIT, READ_ONLY, WRITES, Context, clamp, iso
from .write import check_mtime


def register(mcp: MCPServer, ctx: Context) -> None:
    folders = ctx.config.attachment_folders
    max_bytes = ctx.config.max_attachment_bytes
    allowed_folders = ", ".join(f"{f}/" for f in folders)
    allowed_types = ", ".join(sorted(att.CONTENT_TYPES))

    def limits(fn):
        # The allow-list is configurable, so state the effective one in the
        # description the routine reads, not only the default.
        fn.__doc__ = (fn.__doc__ or "") + (
            f"\n\nOn this server: allowed folders {allowed_folders}; types "
            f"{allowed_types}; size cap {max_bytes} bytes."
        )
        return fn

    @mcp.tool(annotations=WRITES)
    @instrumented("vault_write_attachment", mutation=True)
    @limits
    async def vault_write_attachment(
        path: str,
        content_base64: str,
        mode: str = "create",
        expected_mtime_ns: str | None = None,
        vault: str | None = None,
    ) -> dict[str, Any]:
        """Write a binary attachment (an image) into the vault.

        - `path`: vault-relative, under an attachment folder (default
          `Attachments/`), e.g. "Attachments/Places/Vauban.svg". Parent folders
          are created. Allowed types: .png .jpg .jpeg .webp .svg .gif.
        - `content_base64`: the file's raw bytes, standard base64. (For an SVG,
          base64-encode the UTF-8 text.) Max 2 MB decoded by default.
        - `mode`: "create" (default) refuses if the file already exists;
          "overwrite" replaces it.
        - `expected_mtime_ns`: optional, for overwrites. Pass the `mtime_ns`
          STRING from vault_read_attachment (or from a previous
          vault_write_attachment) unchanged; the write fails instead of
          clobbering a newer copy Dropbox delivered meanwhile.

        The bytes must match the extension (checked by file signature). An SVG
        must be well-formed XML with an <svg> root and nothing active: no
        <script>, no <foreignObject>, no on* event attributes, no DOCTYPE, and
        no href or url() other than '#fragment' references.

        The write is atomic. On overwrite the previous bytes are saved first,
        so list_versions / restore_version can undo it.

        Returns {path, bytes, sha256, mtime_ns}; mtime_ns is a string.
        """
        if mode not in {"create", "overwrite"}:
            raise ToolError(f"mode must be 'create' or 'overwrite'; got {mode!r}.")

        target = ctx.vault(vault)
        resolved = ctx.resolve_attachment(target, path, must_exist=False)

        # Cheap bound before decoding: base64 is 4/3 the size of its payload.
        if len(content_base64) > (max_bytes * 4) // 3 + 1024:
            raise ToolError(_too_big(len(content_base64) * 3 // 4, max_bytes))
        data = att.decode_base64(content_base64)
        if len(data) > max_bytes:
            raise ToolError(_too_big(len(data), max_bytes))
        att.validate(data, resolved.suffix)

        if resolved.is_symlink():
            raise ToolError(f"{path!r} is a symlink; refusing to write through it.")
        exists = resolved.exists()
        if exists and not resolved.is_file():
            raise ToolError(f"{path!r} exists and is not a file.")
        if exists and mode == "create":
            raise ToolError(
                f"An attachment already exists at {path!r}. Pass mode='overwrite' "
                "to replace it (ideally with expected_mtime_ns from "
                "vault_read_attachment), or choose another name."
            )
        if not exists and expected_mtime_ns is not None:
            raise ToolError(
                f"No attachment at {path!r}, but expected_mtime_ns was given. It "
                "may have been deleted since you read it -- check with "
                "vault_read_attachment or vault_list_attachments."
            )
        if exists and expected_mtime_ns is not None:
            check_mtime(resolved, expected_mtime_ns, path, reread="vault_read_attachment")

        version_id = ctx.journal.snapshot(target, resolved, op="write")
        target.write_bytes_atomic(resolved, data)

        rel = target.relative(resolved)
        digest = att.sha256(data)
        ctx.journal.record(
            tool="vault_write_attachment",
            vault=target.name,
            path=rel,
            version_id=version_id,
            mode=mode,
            overwrote=version_id is not None,
            bytes=len(data),
            sha256=digest,
        )
        return {
            "vault": target.name,
            "path": rel,
            "bytes": len(data),
            "sha256": digest,
            "mtime_ns": str(resolved.stat().st_mtime_ns),
            "created": not exists,
            "previous_version_id": version_id,
        }

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("vault_read_attachment")
    async def vault_read_attachment(
        path: str, vault: str | None = None, include_content: bool = False
    ) -> dict[str, Any]:
        """Metadata for one attachment, and optionally its bytes.

        Returns {path, bytes, sha256, mtime_ns, content_type}. `mtime_ns` is a
        STRING: pass it to vault_write_attachment's `expected_mtime_ns`
        unchanged. With include_content=True, also `content_base64` (only for
        files within the attachment size cap, 2 MB by default).

        Compare `sha256` with the hash of what you would write to skip
        uploading an unchanged image.
        """
        target = ctx.vault(vault)
        resolved = ctx.resolve_attachment(target, path, must_exist=True)
        data = ctx.read_attachment(target, resolved)
        stat = resolved.stat()
        result: dict[str, Any] = {
            "vault": target.name,
            "path": target.relative(resolved),
            "bytes": len(data),
            "sha256": att.sha256(data),
            "mtime_ns": str(stat.st_mtime_ns),
            "modified": iso(stat.st_mtime),
            "content_type": att.content_type(resolved.suffix),
        }
        if include_content:
            result["content_base64"] = base64.b64encode(data).decode("ascii")
        return result

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("vault_list_attachments")
    @limits
    async def vault_list_attachments(
        folder: str | None = None,
        limit: int = 100,
        sort: str = "modified",
        vault: str | None = None,
    ) -> dict[str, Any]:
        """List attachments (images) under the attachment folders.

        `folder` narrows to one folder (recursive), e.g. "Attachments/Places";
        omit it for every allowed attachment folder. `sort` is "modified"
        (newest first, the default), "path", or "size" (largest first).
        Returns path, size (bytes), modified and mtime_ns for each file.
        """
        if sort not in {"modified", "path", "size"}:
            raise ToolError(f"sort={sort!r} is not valid. Use 'modified', 'path' or 'size'.")
        target = ctx.vault(vault)
        limit = clamp(limit, MAX_LIST_LIMIT)
        roots = target.resolve_attachment_dir(folder, folders=folders)

        entries: list[dict[str, Any]] = []
        for root in roots:
            for path in target.iter_attachments(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue  # Dropbox removed it mid-walk.
                entries.append(
                    {
                        "path": target.relative(path),
                        "size": stat.st_size,
                        "modified": iso(stat.st_mtime),
                        "mtime_ns": str(stat.st_mtime_ns),
                        "_mtime": stat.st_mtime_ns,
                    }
                )

        if sort == "path":
            entries.sort(key=lambda e: e["path"])
        elif sort == "size":
            entries.sort(key=lambda e: -e["size"])
        else:
            entries.sort(key=lambda e: -e["_mtime"])
        for entry in entries:
            del entry["_mtime"]

        return {
            "vault": target.name,
            "folder": folder or allowed_folders,
            "count": min(len(entries), limit),
            "total": len(entries),
            "truncated": len(entries) > limit,
            "attachments": entries[:limit],
        }


def _too_big(size: int, cap: int) -> str:
    return (
        f"The attachment is about {size} bytes, over the {cap}-byte cap. Shrink "
        "it (for an SVG, simplify paths; for a raster, re-encode it smaller)."
    )

