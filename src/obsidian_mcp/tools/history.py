"""Undo: list and restore the version journal's snapshots.

These tools are what make every other mutation in this server reversible. They
read from the state directory, never from the vault, so a note that was deleted
entirely can still be listed and restored.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..errors import ToolError
from ..telemetry import instrumented
from ._common import MAX_LIST_LIMIT, READ_ONLY, WRITES, Context, clamp


def register(mcp: MCPServer, ctx: Context) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @instrumented("list_versions")
    async def list_versions(
        path: str, vault: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """List saved versions of a note, newest first.

        A version is written before every append, edit, overwrite, and delete.
        The note itself does not need to still exist -- use this to find a
        deleted note's contents.
        """
        target = ctx.vault(vault)
        # must_exist=False: the whole point is that this works after a delete.
        resolved = target.resolve(path, must_exist=False)
        rel = target.relative(resolved)
        versions = ctx.journal.list_versions(target, rel, limit=clamp(limit, MAX_LIST_LIMIT))
        return {
            "vault": target.name,
            "path": rel,
            "exists_now": resolved.is_file(),
            "count": len(versions),
            "versions": versions,
        }

    @mcp.tool(annotations=WRITES)
    @instrumented("restore_version", mutation=True)
    async def restore_version(
        path: str, version_id: str, vault: str | None = None
    ) -> dict[str, Any]:
        """Restore a note to a previous version.

        Get `version_id` from list_versions. The current contents are
        snapshotted first, so a restore is itself undoable -- you can undo an
        undo. Works on a note that was deleted, recreating it at `path`.
        """
        target = ctx.vault(vault)
        resolved = target.resolve(path, must_exist=False)
        rel = target.relative(resolved)

        try:
            content = ctx.journal.read_version(target, rel, version_id)
        except FileNotFoundError:
            raise ToolError(
                f"No version {version_id!r} for {rel!r}. Call list_versions to "
                "see the available version ids."
            ) from None
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        new_version_id = ctx.journal.snapshot(target, resolved, op="restore")
        target.write_text_atomic(resolved, content)

        ctx.journal.record(
            tool="restore_version",
            vault=target.name,
            path=rel,
            restored_from=version_id,
            version_id=new_version_id,
        )
        return {
            "vault": target.name,
            "path": rel,
            "restored_from": version_id,
            "new_version_id": new_version_id,
        }
