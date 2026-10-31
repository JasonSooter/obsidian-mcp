"""Which vault attachments a published note uses, resolved the way Obsidian does.

publish_site copies these alongside the notes, so a page's images reach the site
with it. Three reference forms are collected:

* frontmatter `image`, `cover`, `banner`, `socialImage` -- a vault path, a bare
  filename, or a `[[wikilink]]` to one (absolute URLs are left alone);
* wiki embeds `![[file.ext]]`, `![[file.ext|300]]`, `![[file.ext#x]]`;
* markdown embeds `![alt](relative/path.ext)`, URL-encoded or `<bracketed>`.

Only image types are collected: those are what the attachment tools manage.
Code spans and fenced blocks are ignored, as for wikilinks.

Resolution follows Obsidian: exact vault path first, then relative to the
note's folder, then the basename -- unique, or else the closest to the note and
then the shortest path. That is the same order links.LinkResolver uses for
notes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .notes import parse, strip_code
from .vault import ATTACHMENT_EXTENSIONS, Vault

# Frontmatter keys that name a page image. The first three are what themes and
# the site's hero banner read; socialImage is the link-preview override.
IMAGE_KEYS = ("image", "cover", "banner", "socialImage")

_WIKI_EMBED = re.compile(r"!\[\[([^\]\[|#^]+)(?:[#^][^\]\[|]*)?(?:\|[^\]\[]*)?\]\]")
# ![alt](target "title") -- target may be <bracketed> to allow spaces.
_MD_EMBED = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[\"'][^)]*[\"'])?\s*\)")
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass(frozen=True)
class Reference:
    """One attachment reference in a note."""

    target: str
    # True for the frontmatter image keys: those get a PNG for link previews.
    is_page_image: bool


def _is_attachment(target: str) -> bool:
    return PurePosixPath(target).suffix.lower() in ATTACHMENT_EXTENSIONS


def _clean_frontmatter_value(value: str) -> str:
    """`![[x.svg]]`, `[[x.svg|alias]]`, `/Attachments/x.svg` -> the path."""
    text = value.strip()
    match = re.fullmatch(r"!?\[\[([^\]|#^]+)(?:[#^|][^\]]*)?\]\]", text)
    if match:
        text = match.group(1)
    return text.strip().lstrip("/")


def collect_references(raw: str) -> list[Reference]:
    """Every image reference in a note, frontmatter first, in order, deduplicated."""
    note = parse(raw)
    found: list[Reference] = []
    seen: set[tuple[str, bool]] = set()

    def add(target: str, page_image: bool) -> None:
        target = target.strip()
        if not target or _URL_SCHEME.match(target) or target.startswith("//"):
            return
        if not _is_attachment(target):
            return
        key = (target, page_image)
        if key not in seen:
            seen.add(key)
            found.append(Reference(target, page_image))

    for key in IMAGE_KEYS:
        value = note.metadata.get(key)
        if isinstance(value, str):
            add(_clean_frontmatter_value(value), True)

    body = strip_code(note.body)
    for match in _WIKI_EMBED.finditer(body):
        add(match.group(1), False)
    for match in _MD_EMBED.finditer(body):
        target = match.group(1)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        add(unquote(target.split("#", 1)[0].split("?", 1)[0]), False)
    return found


class AttachmentIndex:
    """Every attachment-type file in the vault, for resolving references.

    Built once per publish; the vault can change between calls, never within
    one resolution pass that matters.
    """

    def __init__(self, vault: Vault) -> None:
        self._all = sorted(vault.relative(p) for p in vault.iter_attachments())
        # Keys are NFC: a note spells `Abbekås.svg` composed, while macOS
        # filesystems (and Dropbox on them) may hand back the decomposed form.
        # Values stay the on-disk spelling, which is what opens the file.
        self._lookup = {_key(rel): rel for rel in self._all}
        self._by_name: dict[str, list[str]] = {}
        for rel in self._all:
            self._by_name.setdefault(_key(PurePosixPath(rel).name), []).append(rel)

    def resolve(self, target: str, *, source: str) -> str | None:
        """The on-disk vault path ``target`` names, or None."""
        target = unicodedata.normalize("NFC", target.strip().lstrip("/"))
        source = unicodedata.normalize("NFC", source)
        if not target:
            return None
        # 1. Exact vault-relative path.
        hit = self._lookup.get(_key(target))
        if hit:
            return hit
        # 2. Relative to the linking note's folder (./x.png, ../img/x.png).
        source_dir = PurePosixPath(source).parent
        joined = _normalise(source_dir / target)
        if joined is not None:
            hit = self._lookup.get(_key(joined))
            if hit:
                return hit
        # 3. Basename: unique, else nearest the note, then shortest path.
        matches = self._by_name.get(_key(PurePosixPath(target).name), [])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return sorted(
            matches,
            key=lambda rel: (
                PurePosixPath(unicodedata.normalize("NFC", rel)).parent != source_dir,
                len(PurePosixPath(rel).parts),
                len(rel),
                rel,
            ),
        )[0]


def _key(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()


def site_path(rel: str) -> str:
    """Where a vault file lives under content/: its path, in NFC.

    git and the site's existing files use NFC, so writing a decomposed name
    would create a second, invisible-twin file on Linux.
    """
    return unicodedata.normalize("NFC", rel)


def _normalise(path: PurePosixPath) -> str | None:
    """Collapse `..` lexically; None if it climbs above the vault root."""
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def slugify_path(path: str) -> str:
    """Quartz's slugifyFilePath for a non-markdown file.

    Ported from @quartz-community/utils so publish_site can check that the
    built site really contains each attachment at the URL pages link to. Per
    segment of the path without its extension: whitespace -> '-', '&' ->
    '-and-', '%' -> '-percent', drop `?#<>:"|*`, lowercase. A file named like
    its folder becomes `index`. The extension is kept exactly as written.
    """
    path = path.strip("/")
    match = re.search(r"\.[A-Za-z0-9]+$", path)
    ext = match.group(0) if match else ""
    stem = path[: len(path) - len(ext)] if ext else path

    def segment(text: str) -> str:
        text = re.sub(r"\s", "-", text)
        text = text.replace("&", "-and-").replace("%", "-percent")
        text = re.sub(r'[?#<>:"|*]', "", text)
        return text.lower()

    slug = "/".join(segment(s) for s in stem.split("/")).rstrip("/")
    if slug.endswith("_index"):
        slug = slug[: -len("_index")] + "index"
    parts = slug.split("/")
    if len(parts) >= 2 and parts[-1] == parts[-2]:
        parts[-1] = "index"
        slug = "/".join(parts)
    return slug + ext


def png_sibling(rel: str) -> str:
    """`Attachments/Places/Vauban.svg` -> `Attachments/Places/Vauban.png`."""
    return str(Path(rel).with_suffix(".png").as_posix())
