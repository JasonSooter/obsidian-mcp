"""Quartz publishing: stage flagged notes into the site repo, validate, push.

This does NOT drive the Quartz Syncer Obsidian plugin. It cannot: that plugin's
CLI requires a running Obsidian app, and this server is headless. What it does
instead is reproduce the plugin's *contract*, which is only two things --

  1. a note opts in with `publish: true` in its frontmatter (the property
     Quartz's built-in ExplicitPublish filter reads), and
  2. the opted-in markdown is pushed to the site's git repo.

Both are things a headless server can do directly.

`publish_site` is the one action in this entire server that cannot be undone,
because public is public. Hence: dry_run defaults to True, the build must
succeed before anything is committed, and every real run is written to the audit
log with its commit sha.
"""

from __future__ import annotations

import fcntl
import logging
import re
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from datetime import datetime, timezone

from .config import QuartzConfig
from .errors import ToolError
from .notes import compose, parse
from .vault import Vault

log = logging.getLogger(__name__)

PUBLISH_KEY = "publish"
BUILD_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 120

# Where staged markdown lands inside the Quartz repo. Quartz v4's convention.
CONTENT_SUBDIR = "content"

# The lock lives in the state directory, NOT the Quartz repo: anything we
# create inside that repo shows up in the user's `git status` and would be
# swept into a commit by `git add -A`.
LOCK_FILENAME = "quartz-publish.lock"

# A note edited within this window of its publishing commit is treated as
# already published. The plugin writes the file, then commits a moment later,
# so an exact comparison would republish everything it just published.
PUBLISH_GRACE_SECONDS = 900


def is_published(raw: str) -> bool:
    """Whether a note's frontmatter opts it in to the public site."""
    value = parse(raw).metadata.get(PUBLISH_KEY)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


# --- frontmatter rendering --------------------------------------------------
#
# The site's content/ directory is also written by the Quartz Syncer Obsidian
# plugin, so what we emit has to match what it emits or the two publishers
# rewrite each other's files forever. Derived by comparing 42 already-published
# notes against their vault originals:
#
#   publish, title, created, modified, published, <original keys in order>
#
#   created   = file birthtime      modified = file mtime
#   published = identical to modified (42/42 of the published set)
#
# We deviate on `created`, deliberately. Birthtime does not survive the rclone
# copy onto the server -- it becomes the time rclone wrote the file -- so it is not
# available here and would be wrong if we used it. Instead `created` is taken
# from the already-published copy and never regenerated, falling back to the
# vault's own `date created` and then to mtime for a note being published for
# the first time. That also makes it immutable once published, which is what a
# creation date should be.

CREATED_FALLBACK_KEYS = ("created", "created_at", "date created", "date")
TIMESTAMP_KEYS = ("created", "modified", "published")
LEAD_KEYS = ("publish", "title")

# Blocks whose published form is *generated*, not copied. The Quartz Syncer
# plugin executes these inside Obsidian and inlines the result; this server has
# no plugin runtime and would publish the query text itself, which Quartz then
# renders as a literal code block -- a visible regression on the live page.
# Such notes are skipped and left as Syncer last published them.
# NOT ```query: Obsidian's core search blocks are passed through verbatim by
# the plugin too, so we can publish those notes normally. Verified against the
# live site, where a ```query block appears unchanged.
DYNAMIC_BLOCK = re.compile(
    r"^\s*```+\s*(dataview|dataviewjs|datacore|datacorejs)\b",
    re.MULTILINE | re.IGNORECASE,
)


def has_dynamic_blocks(raw: str) -> bool:
    """Whether this note's published form depends on an Obsidian plugin."""
    return bool(DYNAMIC_BLOCK.search(raw))


def _iso(when: float) -> str:
    """UTC, milliseconds, trailing Z -- the shape Syncer writes.

    Milliseconds come from the datetime's microseconds rather than from
    `when % 1`: binary floating point cannot represent most fractions exactly,
    so the float route truncated 1786106798.001 to `.000Z`. Deriving both parts
    from the same datetime also guarantees they cannot disagree.
    """
    moment = datetime.fromtimestamp(when, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _existing_created(content_path: Path) -> str | None:
    """The `created` value already on the site, if this note is published."""
    if not content_path.is_file():
        return None
    existing = parse(content_path.read_text(encoding="utf-8", errors="replace"))
    value = existing.metadata.get("created")
    return str(value) if value else None


# Characters the Syncer plugin backslash-escapes on publish. We do not escape,
# and under GFM the escaped and bare forms render identically -- so for the
# purpose of "did this note actually change?", they are the same text.
_ESCAPED = re.compile(r"\\([_*`\[\]()#+\-.!~|<>])")

GENERATED_KEYS = ("created", "modified", "published")


# The Syncer plugin serialises every note through mdast-util-to-markdown, whose
# options are visible in its bundle: emphasis "_", bullet "-". Matching those
# two is cheap and covers the most visible formatting difference. The rest of
# that serializer's behaviour -- escaping above all -- is deliberately not
# reproduced; see the README on why byte-parity is not the goal.
# CommonMark allows a fence to be indented up to three spaces, and the closing
# fence need not match that indent. Anchoring at column 0 left indented blocks
# unmasked, so emphasis and bullets inside them were rewritten -- corrupting the
# published note, which is the exact failure this masking exists to prevent.
_FENCE = re.compile(
    r"^ {0,3}(```+|~~~+).*?(?:^ {0,3}\1[^\S\n]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TRIPLE = re.compile(r"(?<![*\w])\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*(?![*\w])", re.DOTALL)
_SINGLE = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", re.DOTALL)
_BULLET = re.compile(r"^(\s*)[*+](\s+)", re.MULTILINE)


def normalise_markdown(body: str) -> str:
    """Emphasis with underscores and `-` list bullets, as the plugin writes them.

    Code spans are masked out first: an asterisk inside a fenced block or inline
    code is content, not emphasis, and rewriting it would corrupt the note.
    """
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    masked = _INLINE_CODE.sub(stash, _FENCE.sub(stash, body))
    masked = _TRIPLE.sub(r"_**\1**_", masked)
    masked = _SINGLE.sub(r"_\1_", masked)
    masked = _BULLET.sub(r"\1-\2", masked)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], masked)


def _semantic_key(text: str) -> tuple:
    """What the note *means*, ignoring how either publisher chose to spell it.

    Two publishers write this directory: this server and the Quartz Syncer
    Obsidian plugin. Neither owns it. They format differently -- key order,
    backslash escaping, blank runs, millisecond precision -- and if each
    rewrote the other's formatting, every publish from either side would
    produce a 40-file diff and a rebuild.

    So equality here is deliberately loose: frontmatter compared as an
    unordered mapping with the generated timestamps dropped, body compared with
    escaping and blank runs normalised. If two files agree on that, the note
    did not change, and whichever bytes are already on the site are left
    exactly as they are.
    """
    note = parse(text)
    meta = tuple(
        sorted(
            (k, str(v))
            for k, v in note.metadata.items()
            if k not in GENERATED_KEYS
        )
    )
    body = _ESCAPED.sub(r"\1", note.body)
    # The plugin rewrites leading tabs as spaces, so a note indented with tabs
    # in the vault would otherwise look changed on every single run.
    body = re.sub(
        r"^[ \t]+",
        lambda m: " " * len(m.group(0).replace("\t", "  ")),
        body,
        flags=re.MULTILINE,
    )
    body = re.sub(r"[ \t]+$", "", body, flags=re.MULTILINE)
    body = re.sub(r"\n{2,}", "\n\n", body).strip()
    return (meta, body)


def render_for_publish(raw: str, mtime: float, content_path: Path) -> str:
    """Transform a vault note into the bytes the site expects."""
    note = parse(raw)
    original = dict(note.metadata)

    modified = _iso(mtime)
    created = _existing_created(content_path)
    if not created:
        for key in CREATED_FALLBACK_KEYS:
            if original.get(key):
                created = str(original[key])
                break
    if not created:
        created = modified

    ordered: dict[str, object] = {}
    for key in LEAD_KEYS:
        if key in original:
            ordered[key] = original[key]
    ordered["created"] = created
    ordered["modified"] = modified
    ordered["published"] = modified
    for key, value in original.items():
        if key not in ordered:
            ordered[key] = value

    rendered = compose(ordered, normalise_markdown(note.body))

    # Leave an unchanged note exactly as it is on the site, whoever wrote it.
    # See _semantic_key: this is what lets two publishers share the directory
    # without rewriting each other's formatting on every run.
    if content_path.is_file():
        current = content_path.read_text(encoding="utf-8", errors="replace")
        if _semantic_key(current) == _semantic_key(rendered):
            return current

    return rendered


@dataclass
class PublishPlan:
    added: list[str]
    updated: list[str]
    removed: list[str]
    # Notes we deliberately did not touch -- see DYNAMIC_BLOCK.
    skipped: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.updated or self.removed)

    def as_dict(self) -> dict:
        out = {
            "added": sorted(self.added),
            "updated": sorted(self.updated),
            "removed": sorted(self.removed),
            "total_changes": len(self.added) + len(self.updated) + len(self.removed),
        }
        if self.skipped:
            out["skipped"] = sorted(self.skipped)
            out["skipped_reason"] = (
                "These notes contain dataview or datacore blocks, whose published "
                "form is generated by an Obsidian plugin. This server has no plugin "
                "runtime, so publishing them would replace the generated content with "
                "the raw query. They were left exactly as they are on the site. "
                "(```query blocks are NOT skipped: those are Obsidian core search "
                "blocks, which the plugin also publishes verbatim.)"
            )
        return out


class Publisher:
    """Owns the Quartz repo working tree."""

    def __init__(self, config: QuartzConfig, lock_dir: Path) -> None:
        self._config = config
        self._lock_dir = lock_dir

    @property
    def content_dir(self) -> Path:
        return self._config.repo_path / CONTENT_SUBDIR

    @property
    def remote(self) -> str:
        return self._config.remote

    # -- locking -----------------------------------------------------------

    @contextmanager
    def lock(self):
        """Serialise the whole build-commit-push sequence.

        Two concurrent publish_site calls would otherwise race on one git
        working tree and produce a commit containing half of each.
        """
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_dir / LOCK_FILENAME
        with lock_path.open("w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolError(
                    "A publish is already in progress. Wait for it to finish "
                    "and try again -- two publishes cannot share the repo."
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # -- staging -----------------------------------------------------------

    def published_at(self) -> dict[str, float]:
        """When each content file was last committed, by vault-relative path.

        This is the primary "does this note need publishing?" test, and it is
        deliberately not a comparison of file contents. Two publishers write
        this directory and format differently; comparing bytes means one
        rewrites the other's formatting forever, and every publish from either
        side becomes a 40-file diff. Comparing *when* a note was last published
        against when it was last edited is immune to that -- whoever published
        last set the commit time, whatever formatting they used.
        """
        result = subprocess.run(
            ["git", "log", "--format=%ct", "--name-only", "--no-merges",
             "--", CONTENT_SUBDIR],
            cwd=self._config.repo_path, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
        when: dict[str, float] = {}
        stamp = 0.0
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.isdigit():
                stamp = float(line)
            elif line.startswith(CONTENT_SUBDIR + "/"):
                # First mention wins: git log is newest-first.
                when.setdefault(line[len(CONTENT_SUBDIR) + 1 :], stamp)
        return when

    def stage(self, vault: Vault, published: dict[str, tuple[str, float]]) -> PublishPlan:
        """Mirror the published notes into the repo's content directory.

        ``published`` maps vault-relative path -> (contents, mtime). Anything in the
        content directory that is no longer published is removed, so unflagging
        a note actually takes it off the site.
        """
        self.content_dir.mkdir(parents=True, exist_ok=True)
        plan = PublishPlan(added=[], updated=[], removed=[])
        last_published = self.published_at()

        for rel, (content, mtime) in published.items():
            if has_dynamic_blocks(content):
                plan.skipped.append(rel)
                continue

            # Unchanged since it was last published, by anyone: leave it alone.
            # A small grace window absorbs the gap between a file being written
            # and the commit that published it.
            was = last_published.get(rel)
            if was is not None and mtime <= was + PUBLISH_GRACE_SECONDS:
                continue
            target = self.content_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            rendered = render_for_publish(content, mtime, target)
            if not target.exists():
                plan.added.append(rel)
            elif target.read_text(encoding="utf-8", errors="replace") != rendered:
                plan.updated.append(rel)
            else:
                continue
            target.write_text(rendered, encoding="utf-8")

        # Sweep: previously published notes that are no longer flagged.
        for existing in self.content_dir.rglob("*.md"):
            rel = existing.relative_to(self.content_dir).as_posix()
            if rel not in published:
                existing.unlink()
                plan.removed.append(rel)

        return plan

    # -- build -------------------------------------------------------------

    def build(self) -> None:
        """Run the Quartz build as a gate. A broken note must never ship.

        Skipped when QUARTZ_BUILD_LOCALLY=false, which is the right setting when
        a CI workflow builds on push -- in that case the local build would be
        duplicated work and Node would not need to be in this image at all.
        """
        if not self._config.build_locally:
            log.info("local Quartz build disabled; relying on CI to build")
            return
        try:
            completed = subprocess.run(
                ["npx", "quartz", "build"],
                cwd=self._config.repo_path,
                capture_output=True,
                text=True,
                timeout=BUILD_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            raise ToolError(
                "npx is not available in this container, so the Quartz build "
                "cannot run. Either install Node in the image or set "
                "QUARTZ_BUILD_LOCALLY=false to let CI build on push."
            ) from None
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"The Quartz build exceeded {BUILD_TIMEOUT_SECONDS}s and was "
                "killed. Nothing has been committed or pushed."
            ) from None

        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            detail = "\n".join(tail[-15:]) or "no output"
            raise ToolError(
                "The Quartz build failed, so nothing was committed or pushed. "
                "Fix the note that broke it and try again.\n\n" + detail
            )

    # -- git ---------------------------------------------------------------

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        key = self._config.ssh_key_path
        if key:
            # Repo-scoped deploy key, and StrictHostKeyChecking left on: a
            # publish that cannot verify the host should fail, not proceed.
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key} -o IdentitiesOnly=yes "
                "-o UserKnownHostsFile=/state/known_hosts"
            )
        author = self._config.git_author
        name, _, email = author.partition("<")
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name.strip() or "obsidian-mcp"
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = (
            email.rstrip(">").strip() or "obsidian-mcp@localhost"
        )
        return env

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self._config.repo_path,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=self._git_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(
                f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s."
            ) from None

    def diff_stat(self) -> str:
        result = self._git("status", "--porcelain", CONTENT_SUBDIR)
        return result.stdout.strip()

    def commit_and_push(self, message: str) -> str:
        """Commit the staged content and push. Returns the commit sha."""
        add = self._git("add", CONTENT_SUBDIR)
        if add.returncode != 0:
            raise ToolError(f"git add failed: {add.stderr.strip()}")

        commit = self._git("commit", "-m", message)
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr).lower():
                raise ToolError(
                    "Nothing to commit -- the site content already matches the "
                    "published notes."
                )
            raise ToolError(f"git commit failed: {commit.stderr.strip()}")

        push = self._git("push", self._config.remote, f"HEAD:{self._config.branch}")
        if push.returncode != 0:
            # The commit exists locally; only the push failed. Say so, because
            # the recovery is different from 'nothing happened'.
            raise ToolError(
                "The commit was created locally but the push failed, so the "
                "site is unchanged. Check the deploy key and network.\n\n"
                + push.stderr.strip()
            )

        sha = self._git("rev-parse", "HEAD").stdout.strip()
        return sha

    def revert_staging(self) -> None:
        """Undo staged content changes after a failed build or a dry run."""
        self._git("checkout", "--", CONTENT_SUBDIR)
        self._git("clean", "-fd", CONTENT_SUBDIR)


def scratch_clean(path: Path) -> None:
    """Remove a build output directory, ignoring absence."""
    shutil.rmtree(path, ignore_errors=True)
