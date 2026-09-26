"""Shared context and serialisers for the tool modules.

Tools address notes by *vault-relative path*, which is what a model has in hand
after a search or a listing. Nothing here caches note contents: `Context` holds
configuration and handles, never file bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

from ..config import Config
from ..journal import Journal
from ..notes import Note, parse
from ..quartz import Publisher
from ..vault import Vault, VaultRegistry

# Advertised so clients can tell at a glance which tools are safe to call
# speculatively.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
# vault_delete is the only tool flagged destructive. It is still reversible --
# it moves to .trash/ and snapshots to the journal first -- but the annotation
# should reflect user intent, not our recovery machinery.
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

MAX_SEARCH_LIMIT = 200
MAX_LIST_LIMIT = 500


@dataclass(frozen=True)
class Context:
    """Everything the tools need, assembled once at startup."""

    config: Config
    vaults: VaultRegistry
    journal: Journal
    publisher: Publisher | None

    def vault(self, name: str | None) -> Vault:
        return self.vaults.get(name)

    def read(self, vault: Vault, path: Path) -> str:
        """Read a note fresh. The only read path used by tools."""
        return vault.read_text(path, max_bytes=self.config.max_file_bytes)


def clamp(value: int, maximum: int, *, minimum: int = 1) -> int:
    return max(minimum, min(value, maximum))


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def summarize(vault: Vault, path: Path, note: Note | None = None) -> dict[str, Any]:
    """Path + stat metadata for listings. Does not include the body."""
    stat = path.stat()
    summary: dict[str, Any] = {
        "path": vault.relative(path),
        "modified": iso(stat.st_mtime),
        "size": stat.st_size,
    }
    if note is not None:
        summary["title"] = note.title
        summary["tags"] = note.tags
    return summary


def matches_filters(
    note: Note,
    *,
    tag: str | None,
    properties: dict[str, Any] | None,
) -> bool:
    """Whether a note satisfies the tag and frontmatter filters.

    Tag comparison is case-insensitive and matches nested tags by prefix, so
    `tag="project"` finds `#project/home` -- which is how Obsidian's own tag
    pane behaves.
    """
    if tag:
        wanted = tag.lstrip("#").strip().lower()
        if not any(
            t.lower() == wanted or t.lower().startswith(wanted + "/")
            for t in note.tags
        ):
            return False

    if properties:
        for key, expected in properties.items():
            if key not in note.metadata:
                return False
            actual = note.metadata[key]
            if isinstance(actual, (list, tuple)):
                if not any(_loose_equal(item, expected) for item in actual):
                    return False
            elif not _loose_equal(actual, expected):
                return False
    return True


def _loose_equal(actual: Any, expected: Any) -> bool:
    """Compare frontmatter values tolerantly.

    YAML gives us real booleans and ints, while a model calling the tool will
    often pass the string "true" or "2026". Being strict here would make the
    property filter fail in a way that is invisible and hard to debug.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return _as_bool(actual) == _as_bool(expected)
    return str(actual).strip().lower() == str(expected).strip().lower()


def _as_bool(value: Any) -> bool | Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return value


def load_note(ctx: Context, vault: Vault, path: Path) -> Note:
    return parse(ctx.read(vault, path))
