"""The security kernel: every filesystem access in this server goes through here.

No other module may import ``open``, ``Path.write_text``, ``shutil``, or ``os``
for path purposes. That rule is the whole point -- it means the confinement
argument can be audited by reading one file, rather than by proving a negative
about the entire codebase.

The threat model is specific: this server is published to the public internet
via Tailscale Funnel, so ``path`` arguments are attacker-controlled strings. The
vault directory is the entire reachable filesystem.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import PathRejected, VaultUnknown

# Reads and writes alike are restricted to these. Extension checks are not a
# security boundary on their own -- confinement is -- but they stop the server
# being turned into a general-purpose file reader for whatever else the Dropbox
# sidecar happens to drop into the vault directory.
ALLOWED_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".canvas"})

# Binary attachments: reachable only through resolve_attachment(), which also
# confines them to the configured attachment folders. Kept separate from
# ALLOWED_EXTENSIONS so no text tool can be pointed at an image, and no image
# tool at a note.
ATTACHMENT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"})

# Never readable, listable, or searchable. `.obsidian/` holds plugin state and
# plausibly plugin API keys; it is machinery, not knowledge base. `.trash/` is
# excluded from listings so deleted notes do not come back as search hits, but
# vault_delete still writes into it.
EXCLUDED_DIRS = frozenset({".obsidian", ".trash", ".git", ".stfolder", ".sync"})

# Temp files for atomic replace. Named distinctively so a sync tool (Dropbox, rclone) can
# be told to ignore the pattern -- see README. They live in the destination
# directory because os.replace() is only atomic within one filesystem.
TEMP_PREFIX = ".obsidian-mcp.tmp."


@dataclass(frozen=True)
class Vault:
    """One vault root. ``root`` is fully resolved at construction."""

    name: str
    root: Path

    def resolve(self, rel: str, *, must_exist: bool) -> Path:
        """Map a caller-supplied relative path to a real path inside this vault.

        Raises PathRejected for anything that escapes, is the wrong file type,
        or touches an excluded directory. This is the only way to turn a string
        from the wire into a path this server will open.
        """
        return self._resolve(rel, must_exist=must_exist, extensions=ALLOWED_EXTENSIONS)

    def resolve_attachment(
        self, rel: str, *, must_exist: bool, folders: tuple[str, ...]
    ) -> Path:
        """resolve() for a binary attachment, confined to ``folders``.

        Same containment and symlink rules as notes, plus two more: the path
        must sit under one of the configured attachment folders, and -- for a
        file being created -- no component may differ only in case from an
        existing sibling. Linux would happily create `attachments/` beside
        `Attachments/`; Dropbox on a case-insensitive Mac then fails the whole
        sync on the collision.
        """
        resolved = self._resolve(
            rel, must_exist=must_exist, extensions=ATTACHMENT_EXTENSIONS
        )
        parts = resolved.relative_to(self.root).parts
        if not any(self._under(parts, folder) for folder in folders):
            allowed = ", ".join(f"{f}/" for f in folders) or "(none configured)"
            raise PathRejected(
                f"Refusing {rel!r}: attachments may only live under {allowed}."
            )
        if not must_exist:
            self._assert_no_case_collision(resolved, rel)
        return resolved

    def resolve_any_attachment(self, rel: str) -> Path:
        """An existing attachment anywhere in the vault, for publishing only.

        publish_site copies whatever image a published note embeds, wherever it
        lives, so this skips the folder allow-list that guards the write tools.
        Containment and symlink rules still apply.
        """
        return self._resolve(rel, must_exist=True, extensions=ATTACHMENT_EXTENSIONS)

    def is_attachment_path(self, rel: str) -> bool:
        """Whether ``rel`` names an attachment type (by extension only)."""
        return Path(rel).suffix.lower() in ATTACHMENT_EXTENSIONS

    def _resolve(self, rel: str, *, must_exist: bool, extensions: frozenset[str]) -> Path:
        if not rel or not rel.strip():
            raise PathRejected("Path is empty. Give a path relative to the vault root.")
        if "\x00" in rel:
            raise PathRejected("Path contains a null byte.")

        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive:
            raise PathRejected(
                f"Path {rel!r} is absolute. Paths must be relative to the vault "
                "root, e.g. 'Inbox/2026-08-28.md'."
            )

        if candidate.suffix.lower() not in extensions:
            raise PathRejected(
                f"Refusing to touch {rel!r}: only "
                f"{', '.join(sorted(extensions))} files are accessible here."
            )

        joined = self.root / candidate

        # resolve() collapses '..' AND follows symlinks, so a symlink planted
        # inside the vault that points at /etc/passwd is caught by exactly the
        # same check as '../../etc/passwd'. Doing these separately is how this
        # kind of code usually grows a hole.
        #
        # For a file that does not exist yet we resolve the parent instead:
        # strict=False would happily return a path through a symlinked parent
        # without telling us.
        if must_exist:
            resolved = joined.resolve()
            probe = resolved
        else:
            parent = joined.parent.resolve()
            probe = parent
            resolved = parent / joined.name

        self._assert_contained(probe, rel)
        self._assert_not_excluded(resolved, rel)

        if must_exist and not resolved.is_file():
            raise PathRejected(
                f"No note at {rel!r}. Use vault_list or search_query to find the "
                "current path -- it may have been moved or renamed."
            )
        return resolved

    def resolve_dir(self, rel: str | None) -> Path:
        """Resolve a folder argument, defaulting to the vault root."""
        if rel is None or not rel.strip() or rel.strip() in {".", "/"}:
            return self.root
        candidate = Path(rel)
        if candidate.is_absolute() or candidate.drive:
            raise PathRejected(f"Folder {rel!r} is absolute; use a relative folder.")
        resolved = (self.root / candidate).resolve()
        self._assert_contained(resolved, rel)
        self._assert_not_excluded(resolved, rel)
        if not resolved.is_dir():
            raise PathRejected(f"No folder at {rel!r} in vault {self.name!r}.")
        return resolved

    def relative(self, path: Path) -> str:
        """Vault-relative POSIX string for a resolved path. For output only."""
        return path.relative_to(self.root).as_posix()

    def resolve_attachment_dir(self, rel: str | None, *, folders: tuple[str, ...]) -> list[Path]:
        """Folders an attachment listing may walk: ``rel``, or every allowed one."""
        if rel is None or not rel.strip() or rel.strip() in {".", "/"}:
            return [
                path for folder in folders
                if (path := (self.root / folder).resolve()).is_dir()
                and self._contained(path)
            ]
        resolved = self.resolve_dir(rel)
        parts = resolved.relative_to(self.root).parts
        if not any(self._under(parts, folder) for folder in folders):
            allowed = ", ".join(f"{f}/" for f in folders) or "(none configured)"
            raise PathRejected(f"Folder {rel!r} is not under {allowed}.")
        return [resolved]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _under(parts: tuple[str, ...], folder: str) -> bool:
        prefix = Path(folder).parts
        return len(parts) > len(prefix) and parts[: len(prefix)] == prefix

    def _contained(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _assert_no_case_collision(self, resolved: Path, original: str) -> None:
        # Compares directory entries by name rather than asking exists(): on a
        # case-insensitive filesystem (macOS) exists() says yes to the wrong
        # spelling, which would hide exactly the twin this guards against.
        current = self.root
        for part in resolved.relative_to(self.root).parts:
            if not current.is_dir():
                break
            clash = next(
                (
                    entry.name for entry in current.iterdir()
                    if entry.name.lower() == part.lower() and entry.name != part
                ),
                None,
            )
            if clash is not None:
                raise PathRejected(
                    f"Refusing {original!r}: {clash!r} already exists and differs "
                    f"only in case from {part!r}. Use the existing spelling -- a "
                    "case-only twin breaks Dropbox sync on macOS."
                )
            current = current / part

    def _assert_contained(self, resolved: Path, original: str) -> None:
        try:
            resolved.relative_to(self.root)
        except ValueError:
            # Note what this does NOT say: nothing about where the path landed,
            # or whether the target exists. A public endpoint must not be usable
            # as an oracle for probing the host filesystem.
            raise PathRejected(
                f"Path {original!r} resolves outside the vault and was rejected."
            ) from None

    def _assert_not_excluded(self, resolved: Path, original: str) -> None:
        try:
            parts = resolved.relative_to(self.root).parts
        except ValueError:  # pragma: no cover -- containment already checked
            raise PathRejected(f"Path {original!r} was rejected.") from None
        for part in parts:
            if part in EXCLUDED_DIRS:
                raise PathRejected(
                    f"Path {original!r} is inside {part!r}, which is not part of "
                    "the knowledge base and is never accessible."
                )
            if part.startswith(TEMP_PREFIX):
                raise PathRejected(
                    f"Path {original!r} names an in-flight temporary file."
                )

    # -- the only IO primitives in the codebase ----------------------------

    def read_text(self, path: Path, *, max_bytes: int) -> str:
        """Read a confined path fresh from disk.

        Nothing is cached, here or anywhere: Dropbox is a second writer to this
        directory, so a cached body could be stale the instant it is stored.
        """
        size = path.stat().st_size
        if size > max_bytes:
            raise PathRejected(
                f"{self.relative(path)} is {size} bytes, over the "
                f"{max_bytes}-byte limit. Refusing to load it."
            )
        return path.read_text(encoding="utf-8", errors="replace")

    def read_bytes(self, path: Path, *, max_bytes: int) -> bytes:
        """read_text() for binary attachments: fresh from disk, size-capped."""
        size = path.stat().st_size
        if size > max_bytes:
            raise PathRejected(
                f"{self.relative(path)} is {size} bytes, over the "
                f"{max_bytes}-byte limit. Refusing to load it."
            )
        return path.read_bytes()

    def write_text_atomic(self, path: Path, content: str) -> None:
        """Replace a file's contents atomically.

        Temp file in the same directory (so os.replace is a same-filesystem
        rename), fsync before the rename (so a power loss cannot leave a
        truncated note), then replace.

        This is atomic for readers on this host. It is NOT invisible to Dropbox,
        which sees a delete plus a create rather than a modify -- see the README
        note about conflicted copies.
        """
        self.write_bytes_atomic(path, content.encode("utf-8"), temp_suffix=".md")

    def write_bytes_atomic(self, path: Path, data: bytes, *, temp_suffix: str = ".tmp") -> None:
        """write_text_atomic() for raw bytes. Dropbox never sees a partial file.

        Attachments use a neutral ``.tmp`` suffix so Obsidian does not index
        the in-flight file as an image during the instant it exists.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=TEMP_PREFIX, suffix=temp_suffix
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def iter_attachments(self, start: Path | None = None):
        """iter_notes() for attachment types."""
        yield from self._iter(start, ATTACHMENT_EXTENSIONS)

    def iter_notes(self, start: Path | None = None):
        """Yield every allowed note under ``start``, skipping excluded dirs.

        Prunes excluded directories during the walk rather than filtering
        afterwards, so we never even stat inside `.obsidian/`.
        """
        yield from self._iter(start, ALLOWED_EXTENSIONS)

    def _iter(self, start: Path | None, extensions: frozenset[str]):
        root = start or self.root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIRS and not d.startswith(TEMP_PREFIX)
            ]
            for filename in filenames:
                if filename.startswith(TEMP_PREFIX):
                    continue
                if Path(filename).suffix.lower() in extensions:
                    yield Path(dirpath) / filename


class VaultRegistry:
    """The configured vaults, by name."""

    def __init__(self, vaults: dict[str, Vault], default: str) -> None:
        self._vaults = vaults
        self._default = default

    @property
    def default_name(self) -> str:
        return self._default

    def get(self, name: str | None) -> Vault:
        """Look up a vault, or raise an error naming the valid ones.

        Same self-correcting-error idiom as anki-mcp's resolve_deck_id: a model
        that guesses wrong can fix itself in one turn.
        """
        key = name or self._default
        vault = self._vaults.get(key)
        if vault is None:
            available = ", ".join(sorted(self._vaults))
            raise VaultUnknown(
                f"No vault named {key!r}. This server is configured with: {available}."
            )
        return vault

    def all(self) -> list[Vault]:
        return list(self._vaults.values())


def build_registry(root: Path, names: tuple[str, ...], default: str) -> VaultRegistry:
    """Resolve and validate every configured vault. Called once, at startup."""
    resolved_root = root.resolve()
    vaults: dict[str, Vault] = {}
    for name in names:
        vault_path = (resolved_root / name).resolve()
        try:
            vault_path.relative_to(resolved_root)
        except ValueError:
            raise ValueError(
                f"Vault {name!r} resolves to {vault_path}, outside "
                f"OBSIDIAN_VAULT_ROOT ({resolved_root})."
            ) from None
        if not vault_path.is_dir():
            raise ValueError(f"Vault {name!r} is not a directory: {vault_path}")
        vaults[name] = Vault(name=name, root=vault_path)
    return VaultRegistry(vaults, default)
