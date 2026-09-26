"""Full-text search, driven by ripgrep.

Why a subprocess rather than a Python walk or a persistent index: the vault is
shared mutable state (Dropbox writes to it whenever it likes), so the design
rule is read-fresh-every-call. An index would need invalidating against a writer
we do not control. ripgrep re-reads the vault from disk on every query, which is
exactly the semantics we want, and is fast enough that we can afford it.

ripgrep is also the safer way to run a caller-supplied pattern: it is invoked as
an argv list with no shell, and `--regexp` is passed as a separate argument so a
query beginning with a dash cannot become a flag.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolError
from .vault import ALLOWED_EXTENSIONS, EXCLUDED_DIRS, TEMP_PREFIX, Vault

log = logging.getLogger(__name__)

# A runaway regex against a large vault must not hang the server.
SEARCH_TIMEOUT_SECONDS = 30

# Cap what ripgrep will even consider, independent of the caller's limit.
RG_MAX_COUNT_PER_FILE = 5


@dataclass(frozen=True)
class RawHit:
    path: Path
    line_number: int
    line_text: str


def _globs() -> list[str]:
    globs: list[str] = []
    for ext in sorted(ALLOWED_EXTENSIONS):
        globs += ["--glob", f"*{ext}"]
    for excluded in sorted(EXCLUDED_DIRS):
        globs += ["--glob", f"!{excluded}/**"]
    globs += ["--glob", f"!{TEMP_PREFIX}*"]
    return globs


def run(
    vault: Vault,
    query: str,
    *,
    root: Path,
    case_sensitive: bool,
    fixed_string: bool,
    max_results: int,
) -> list[RawHit]:
    """Run one ripgrep query and return raw line hits."""
    command = [
        "rg",
        "--json",
        "--max-count", str(RG_MAX_COUNT_PER_FILE),
        "--max-filesize", "10M",
        # Symlinks are deliberately NOT followed: rg must not be the thing that
        # walks out of the vault that vault.py works to keep us inside.
        "--no-follow",
    ]
    command += ["--case-sensitive"] if case_sensitive else ["--ignore-case"]
    if fixed_string:
        command.append("--fixed-strings")
    command += _globs()
    # '--' then the pattern as its own argument: a query like '-foo' is data.
    command += ["--regexp", query, "--", str(root)]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(
            "ripgrep (rg) is not installed in this container, so search cannot "
            "run. This is a server packaging problem, not a problem with the "
            "query."
        ) from None
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"Search timed out after {SEARCH_TIMEOUT_SECONDS}s. Try a more "
            "specific query, or narrow it with the folder argument."
        ) from None

    # rg exits 1 for 'no matches', which is not an error. 2 is a real failure.
    if completed.returncode not in (0, 1):
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[0] if detail else "unknown error"
        if "regex parse error" in message or "unclosed" in message.lower():
            raise ToolError(
                f"That is not a valid regular expression: {message}. Either fix "
                "the pattern or set fixed_string=True to search for it literally."
            )
        raise ToolError(f"Search failed: {message}")

    hits: list[RawHit] = []
    for line in completed.stdout.splitlines():
        if len(hits) >= max_results:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path_text = data.get("path", {}).get("text")
        if not path_text:
            continue
        hits.append(
            RawHit(
                path=Path(path_text),
                line_number=data.get("line_number") or 0,
                line_text=(data.get("lines", {}).get("text") or "").rstrip("\n"),
            )
        )
    return hits


def list_all_paths(vault: Vault, root: Path) -> list[Path]:
    """Every note under ``root``. Used when a query is filter-only."""
    if root == vault.root:
        return list(vault.iter_notes())
    return list(vault.iter_notes(root))


def excerpt(
    line: str, query: str, *, width: int = 200, fixed_string: bool = True
) -> str:
    """A trimmed line centred on the match, for display.

    ``fixed_string`` has to match how the query was handed to ripgrep. Escaping
    a regex query would look for its metacharacters literally, find nothing,
    and silently trim from the start of the line instead of around the hit --
    exactly where a long line is least useful.
    """
    line = line.strip()
    if len(line) <= width:
        return line

    match = None
    if fixed_string:
        match = re.search(re.escape(query), line, re.IGNORECASE)
    else:
        try:
            match = re.search(query, line, re.IGNORECASE)
        except re.error:
            # ripgrep's syntax is a superset of Python's; if we cannot compile
            # it, fall back to a literal search rather than failing the tool.
            match = re.search(re.escape(query), line, re.IGNORECASE)
    centre = match.start() if match else 0
    start = max(0, centre - width // 2)
    end = min(len(line), start + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(line) else ""
    return f"{prefix}{line[start:end]}{suffix}"
