"""Note parsing and composition: frontmatter, tags, headings, appends.

Everything here is pure string work. Reads and writes belong to vault.py; this
module never touches the filesystem, which keeps the append/heading logic
unit-testable without a vault on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import frontmatter
import yaml


class _NoTimestampLoader(yaml.SafeLoader):
    """SafeLoader that leaves date-like scalars as strings.

    PyYAML resolves anything matching its timestamp pattern into a datetime,
    and safe_dump then re-emits it in YAML's own format -- turning
    `2024-06-04T19:23:37-06:00` into `2024-06-04 19:23:37-06:00`. That rewrote
    the reader's frontmatter on every edit, and on the Quartz site it would
    have reformatted the dates of every published note. Obsidian and Quartz
    both treat these as opaque strings, so the fix is to stop parsing them.
    """


class _NoTimestampDumper(yaml.SafeDumper):
    """SafeDumper that writes date-like strings without quotes.

    The loader keeps them as strings; without the matching change here PyYAML
    quotes them on the way out -- because *its* resolver would otherwise read
    the plain scalar back as a datetime -- so `2024-06-04T19:23:37-06:00`
    became `'2024-06-04T19:23:37-06:00'`. Dropping the same resolver from the
    dumper makes the round trip byte-exact.

    It also matches two Obsidian conventions PyYAML does not share: block
    sequences are indented under their key (`tags:\n  - x`, not `tags:\n- x`),
    and an empty value stays empty rather than becoming the literal `null`.
    Both appear in almost every note, so without them every write would rewrite
    the whole frontmatter block.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        # indentless=False keeps block sequences indented under their key.
        return super().increase_indent(flow, False)


def _represent_none(dumper: yaml.SafeDumper, _value: object) -> yaml.Node:
    """`aliases:` with nothing after it, rather than `aliases: null`."""
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    """Double quotes when quoting is needed, matching Obsidian.

    PyYAML reaches for single quotes, so `"[[Places]]"` came back as
    `'[[Places]]'` -- a difference on almost every note with a link in its
    frontmatter.
    """
    style = None
    if value.startswith(("[[", "{", "[", "*", "&", "!", "%", "@", "`")) or ": " in value:
        style = '"'
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class _ObsidianYAMLHandler(frontmatter.YAMLHandler):
    """frontmatter handler that parses with the no-timestamp loader.

    Passing Loader= to frontmatter.loads does not work: its **kwargs are
    default *metadata* values, so the loader class ends up as a frontmatter
    key. The handler is the supported seam.
    """

    def load(self, fm: str, **kwargs: object):  # type: ignore[override]
        kwargs.setdefault("Loader", _NoTimestampLoader)
        return yaml.load(fm, **kwargs)  # type: ignore[arg-type]


# Drop the implicit resolver that turns date-like scalars into datetimes, on
# both sides: the loader so they stay strings, the dumper so they stay unquoted.
def _without_timestamps(resolvers: dict) -> dict:
    return {
        key: [(tag, rx) for tag, rx in entries if tag != "tag:yaml.org,2002:timestamp"]
        for key, entries in resolvers.items()
    }


_NoTimestampLoader.yaml_implicit_resolvers = _without_timestamps(
    yaml.SafeLoader.yaml_implicit_resolvers
)
_NoTimestampDumper.yaml_implicit_resolvers = _without_timestamps(
    yaml.SafeDumper.yaml_implicit_resolvers
)
_NoTimestampDumper.add_representer(type(None), _represent_none)
_NoTimestampDumper.add_representer(str, _represent_str)

# Inline #tags. Excludes bare '#' followed by a digit so that '#1' in prose and
# markdown headings ('# Title' -- space after the hash) are not read as tags.
# Obsidian allows '/' for nested tags and '-'/'_' inside them.
_INLINE_TAG = re.compile(r"(?:^|(?<=\s))#([A-Za-z_][\w/-]*)")

# Fenced blocks and inline code, stripped before scanning for tags or links so
# that examples inside code samples are not mistaken for real ones.
_FENCED = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)", re.MULTILINE | re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def strip_code(text: str) -> str:
    """Blank out code spans, preserving line count so line numbers stay valid."""
    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _INLINE_CODE.sub(blank, _FENCED.sub(blank, text))


@dataclass(frozen=True)
class Note:
    """A parsed note. ``raw`` is exactly what was on disk."""

    raw: str
    metadata: dict[str, Any]
    body: str

    @property
    def title(self) -> str | None:
        for key in ("title", "Title"):
            value = self.metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for line in self.body.splitlines():
            match = _ATX_HEADING.match(line)
            if match:
                return match.group(2)
        return None

    @property
    def tags(self) -> list[str]:
        """Frontmatter tags plus inline #tags, deduped, order-stable.

        Obsidian accepts frontmatter tags as either a list or a comma/space
        separated string, so both are handled.
        """
        found: list[str] = []
        raw_tags = self.metadata.get("tags") or self.metadata.get("tag")
        if isinstance(raw_tags, str):
            found.extend(t for t in re.split(r"[,\s]+", raw_tags) if t)
        elif isinstance(raw_tags, (list, tuple)):
            found.extend(str(t) for t in raw_tags if t)
        found.extend(_INLINE_TAG.findall(strip_code(self.body)))

        seen: set[str] = set()
        result = []
        for tag in found:
            clean = tag.lstrip("#").strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                result.append(clean)
        return result


def parse(raw: str) -> Note:
    """Split frontmatter from body. Malformed YAML degrades to 'no frontmatter'.

    A note with a broken frontmatter block is still a note the user wants to
    read; refusing to parse it would make the tool useless exactly when the user
    is trying to find and fix the problem.
    """
    try:
        post = frontmatter.loads(raw, handler=_ObsidianYAMLHandler())
        return Note(raw=raw, metadata=dict(post.metadata), body=post.content)
    except (yaml.YAMLError, ValueError):
        return Note(raw=raw, metadata={}, body=raw)


def compose(metadata: dict[str, Any] | None, body: str) -> str:
    """Render frontmatter + body back to a file's contents.

    Uses sort_keys=False so an existing note's key order survives a round trip
    -- reordering someone's frontmatter on every edit produces noisy Dropbox
    syncs and noisy git diffs in the Quartz repo.
    """
    if not metadata:
        return _ensure_trailing_newline(body)
    dumped = yaml.dump(
        metadata,
        Dumper=_NoTimestampDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return _ensure_trailing_newline(f"---\n{dumped}\n---\n\n{body.lstrip()}")


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def append_to_body(
    raw: str,
    addition: str,
    *,
    heading: str | None = None,
    ensure_blank_line: bool = True,
) -> str:
    """Return ``raw`` with ``addition`` appended, optionally under a heading.

    With ``heading``, the text is inserted at the end of that section -- after
    the heading's existing content, immediately before the next heading of the
    same or higher level. That is what "append under ## Log" means to a person,
    and it is not the same as appending to the end of the file.

    A named heading that does not exist is created at the end rather than
    raising: from a phone, silently losing a capture is worse than a slightly
    wrong location.
    """
    note = parse(raw)
    addition = addition.strip("\n")

    if heading is None:
        body = _append_plain(note.body, addition, ensure_blank_line)
    else:
        body = _append_under_heading(note.body, addition, heading, ensure_blank_line)

    if note.metadata:
        return compose(note.metadata, body)
    return _ensure_trailing_newline(body)


def _append_plain(body: str, addition: str, blank_line: bool) -> str:
    stripped = body.rstrip("\n")
    if not stripped:
        return addition + "\n"
    separator = "\n\n" if blank_line else "\n"
    return stripped + separator + addition + "\n"


def _append_under_heading(
    body: str, addition: str, heading: str, blank_line: bool
) -> str:
    lines = body.splitlines()
    target = heading.strip().lstrip("#").strip().lower()

    start = None
    level = 0
    for index, line in enumerate(lines):
        match = _ATX_HEADING.match(line)
        if match and match.group(2).strip().lower() == target:
            start = index
            level = len(match.group(1))
            break

    if start is None:
        # Heading absent: create it at the end of the file.
        tail = _append_plain(body, f"## {heading.strip()}", blank_line)
        return _append_plain(tail, addition, blank_line)

    # Walk to the end of the section: the next heading at the same or a higher
    # level (fewer '#'). A deeper subheading is still inside this section.
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _ATX_HEADING.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break

    section = lines[start:end]
    while section and not section[-1].strip():
        section.pop()
    if blank_line:
        section.append("")
    section.append(addition)
    if end < len(lines):
        section.append("")

    return "\n".join(lines[:start] + section + lines[end:])


def set_frontmatter_key(raw: str, key: str, value: Any) -> str:
    """Set (or, with value None, remove) one frontmatter key.

    Preserves the body byte-for-byte and every other key's order, so flipping
    `publish: true` produces a one-line diff in the Quartz repo.
    """
    note = parse(raw)
    metadata = dict(note.metadata)
    if value is None:
        metadata.pop(key, None)
    else:
        metadata[key] = value
    return compose(metadata, note.body)


# --- patch: targeted insertion relative to a heading, block, or property ----

# A block reference: a trailing '^id' marking one block as a link target.
_BLOCK_REF = re.compile(r"\s\^([A-Za-z0-9-]+)\s*$")

# Nested heading paths use '::', matching the Obsidian REST API convention that
# the plugin-based servers accept: "Parent::Child::Grandchild".
HEADING_SEPARATOR = "::"


class PatchTargetMissing(LookupError):
    """The heading, block, or property named by a patch was not found."""


def find_heading_section(lines: list[str], heading_path: str) -> tuple[int, int, int]:
    """Locate a heading and the extent of its section.

    Returns (heading_index, section_end, level). ``heading_path`` may be a plain
    heading, or a '::'-separated path for a nested one -- the segments must
    appear in order and at strictly increasing depth, so 'Log::Today' does not
    match a stray 'Today' somewhere else in the file.
    """
    segments = [s.strip().lstrip("#").strip() for s in heading_path.split(HEADING_SEPARATOR)]
    segments = [s for s in segments if s]
    if not segments:
        raise PatchTargetMissing("empty heading path")

    search_from = 0
    parent_level = 0
    index = -1
    level = 0

    for depth, segment in enumerate(segments):
        index = -1
        for i in range(search_from, len(lines)):
            match = _ATX_HEADING.match(lines[i])
            if not match:
                continue
            current_level = len(match.group(1))
            # Left the parent's section without finding this segment.
            if depth > 0 and current_level <= parent_level:
                break
            if match.group(2).strip().lower() == segment.lower():
                index = i
                level = current_level
                break
        if index < 0:
            raise PatchTargetMissing(heading_path)
        search_from = index + 1
        parent_level = level

    end = len(lines)
    for i in range(index + 1, len(lines)):
        match = _ATX_HEADING.match(lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    return index, end, level


def find_block(lines: list[str], block_id: str) -> int:
    """Index of the line carrying '^block-id', or raise."""
    wanted = block_id.lstrip("^").strip().lower()
    for i, line in enumerate(lines):
        match = _BLOCK_REF.search(line)
        if match and match.group(1).lower() == wanted:
            return i
    raise PatchTargetMissing(f"^{wanted}")


def patch(
    raw: str,
    *,
    operation: str,
    target_type: str,
    target: str,
    content: str,
) -> str:
    """Insert or replace content at a specific place in a note.

    operation:   append | prepend | replace
    target_type: heading | block | frontmatter

    For a heading, 'append' means the end of that section and 'prepend' the line
    just after the heading -- the two things a person means by "add it under
    this heading". 'replace' swaps the section's body, keeping the heading.
    """
    if operation not in {"append", "prepend", "replace"}:
        raise ValueError(f"operation must be append, prepend, or replace; got {operation!r}")
    if target_type not in {"heading", "block", "frontmatter"}:
        raise ValueError(
            f"target_type must be heading, block, or frontmatter; got {target_type!r}"
        )

    note = parse(raw)

    if target_type == "frontmatter":
        metadata = dict(note.metadata)
        if operation == "replace":
            metadata[target] = content
        else:
            existing = metadata.get(target)
            if existing is None:
                metadata[target] = content
            elif isinstance(existing, list):
                metadata[target] = (
                    [*existing, content] if operation == "append" else [content, *existing]
                )
            else:
                metadata[target] = (
                    f"{existing}\n{content}" if operation == "append"
                    else f"{content}\n{existing}"
                )
        return compose(metadata, note.body)

    lines = note.body.splitlines()
    addition = content.strip("\n").splitlines()

    if target_type == "block":
        index = find_block(lines, target)
        if operation == "replace":
            # Keep the block reference itself, or the link pointing here breaks.
            match = _BLOCK_REF.search(lines[index])
            suffix = f" ^{match.group(1)}" if match else ""
            lines[index : index + 1] = [addition[0] + suffix, *addition[1:]]
        elif operation == "append":
            lines[index + 1 : index + 1] = ["", *addition]
        else:
            lines[index:index] = [*addition, ""]
    else:
        start, end, _ = find_heading_section(lines, target)
        if operation == "prepend":
            lines[start + 1 : start + 1] = ["", *addition]
        elif operation == "append":
            section_end = end
            while section_end > start + 1 and not lines[section_end - 1].strip():
                section_end -= 1
            lines[section_end:section_end] = ["", *addition]
        else:
            lines[start + 1 : end] = ["", *addition, ""]

    body = "\n".join(lines)
    return compose(note.metadata, body) if note.metadata else _ensure_trailing_newline(body)


def headings(body: str) -> list[dict[str, Any]]:
    """Outline of a note: every ATX heading with its level and line number."""
    found = []
    for number, line in enumerate(strip_code(body).splitlines(), start=1):
        match = _ATX_HEADING.match(line)
        if match:
            found.append(
                {
                    "level": len(match.group(1)),
                    "text": match.group(2).strip(),
                    "line_number": number,
                }
            )
    return found


def rewrite_link_targets(body: str, old_target: str, new_target: str) -> tuple[str, int]:
    """Repoint every [[wikilink]] whose target is ``old_target``.

    Preserves each link's alias, subpath, and embed marker, so only the target
    changes. Returns (new_body, count). Used by vault_move so renaming a note
    does not leave broken links behind it.
    """
    count = 0
    protected = strip_code(body)

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        # Only rewrite links that survived code-stripping -- a [[link]] inside a
        # fenced example is documentation, not a reference.
        if protected[match.start() : match.end()] != match.group(0):
            return match.group(0)
        if match.group(2).strip().lower() != old_target.lower():
            return match.group(0)
        count += 1
        bang, _, subpath, alias = match.groups()
        rebuilt = f"{bang}[[{new_target}{subpath or ''}"
        if alias is not None:
            rebuilt += f"|{alias}"
        return rebuilt + "]]"

    from .links import _WIKILINK

    return _WIKILINK.sub(replace, body), count
