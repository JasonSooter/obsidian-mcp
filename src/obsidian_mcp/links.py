"""[[wikilink]] parsing and Obsidian's shortest-unique-path resolution.

Matching on filename alone would be wrong often enough to matter. Obsidian
resolves a link by *specificity*: an exact vault-relative path wins, then a path
relative to the linking note, then a unique basename anywhere in the vault. Only
when a basename is ambiguous does proximity decide -- and in that case we report
the alternatives rather than silently picking, because a model that is told the
link is ambiguous can ask, while one that is handed a confident wrong answer
cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .notes import strip_code
from .vault import Vault

# [[target#subpath|alias]] with an optional leading ! for embeds. Non-greedy so
# two links on one line do not merge into one match.
_WIKILINK = re.compile(r"(!?)\[\[([^\]\[|#^]+)((?:[#^][^\]\[|]*)?)(?:\|([^\]\[]*))?\]\]")


@dataclass(frozen=True)
class LinkRef:
    """One [[wikilink]] occurrence in a source note."""

    raw: str
    target: str
    subpath: str | None
    alias: str | None
    is_embed: bool
    line_number: int
    line_text: str


def parse_links(body: str) -> list[LinkRef]:
    """Every wikilink in ``body``, with 1-based line numbers.

    Code is blanked first (preserving line count) so ``[[Example]]`` inside a
    fenced block documenting this very syntax is not counted as a link.
    """
    cleaned = strip_code(body)
    refs: list[LinkRef] = []
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        for match in _WIKILINK.finditer(line):
            target = match.group(2).strip()
            if not target:
                continue
            subpath = (match.group(3) or "").strip() or None
            alias = (match.group(4) or "").strip() or None
            refs.append(
                LinkRef(
                    raw=match.group(0),
                    target=target,
                    subpath=subpath,
                    alias=alias,
                    is_embed=match.group(1) == "!",
                    line_number=line_number,
                    line_text=line.strip(),
                )
            )
    return refs


class LinkResolver:
    """Resolves link targets against a snapshot of the vault's note paths.

    Built fresh per tool call. That is the deliberate cost of the no-cache rule:
    Dropbox can add or rename notes between two calls, and a stale index would
    resolve links to files that no longer exist.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        self._all: list[str] = [vault.relative(p) for p in vault.iter_notes()]
        # Basename (without extension, lowercased) -> every path with that name.
        self._by_stem: dict[str, list[str]] = {}
        for rel in self._all:
            self._by_stem.setdefault(Path(rel).stem.lower(), []).append(rel)
        self._lookup = {rel.lower(): rel for rel in self._all}

    @property
    def all_paths(self) -> list[str]:
        """Every note path in the vault, as of when this resolver was built."""
        return list(self._all)

    def resolve(self, target: str, *, source: str) -> tuple[str | None, list[str]]:
        """Resolve one link target. Returns (resolved_path, alternatives).

        ``alternatives`` is non-empty only when the target was an ambiguous
        basename; it lists every candidate, best guess first.
        """
        target = target.strip().strip("/")
        if not target:
            return None, []

        # 1. Exact vault-relative path, with or without the extension.
        for candidate in self._with_extensions(target):
            hit = self._lookup.get(candidate.lower())
            if hit:
                return hit, []

        # 2. Relative to the folder the linking note lives in -- but only for
        #    a target that actually contains a slash. A *bare* name like
        #    [[Target]] is not a relative path in Obsidian; it goes to the
        #    proximity ranking below, which is what surfaces ambiguity. Treating
        #    a bare name as relative here silently resolves it to the
        #    same-folder file and reports no alternatives, hiding the fact that
        #    a second note of that name exists elsewhere.
        source_dir = Path(source).parent
        if "/" in target and str(source_dir) not in {".", ""}:
            for candidate in self._with_extensions(target):
                joined = (source_dir / candidate).as_posix()
                hit = self._lookup.get(joined.lower())
                if hit:
                    return hit, []

        # 3. Basename match anywhere in the vault.
        matches = self._by_stem.get(Path(target).stem.lower(), [])
        if not matches:
            return None, []
        if len(matches) == 1:
            return matches[0], []

        # 4. Ambiguous: prefer the same folder as the source, then the shortest
        #    path (fewest segments, then fewest characters) -- Obsidian's own
        #    tie-break. Report the rest so the caller knows a choice was made.
        ranked = sorted(
            matches,
            key=lambda rel: (
                Path(rel).parent != source_dir,
                len(Path(rel).parts),
                len(rel),
                rel,
            ),
        )
        return ranked[0], ranked

    def _with_extensions(self, target: str) -> list[str]:
        if Path(target).suffix:
            return [target]
        return [f"{target}.md", target]


def outgoing(vault: Vault, source_rel: str, body: str) -> list[dict]:
    """Resolve every link in one note. Backs the resolve_links tool."""
    resolver = LinkResolver(vault)
    results = []
    for ref in parse_links(body):
        resolved, alternatives = resolver.resolve(ref.target, source=source_rel)
        results.append(
            {
                "raw": ref.raw,
                "target": ref.target,
                "subpath": ref.subpath,
                "alias": ref.alias,
                "is_embed": ref.is_embed,
                "line_number": ref.line_number,
                "resolved_path": resolved,
                "exists": resolved is not None,
                # Present only when the basename matched several notes.
                "ambiguous_candidates": alternatives,
            }
        )
    return results
