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
import unicodedata
from urllib.parse import quote
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from datetime import datetime, timezone

from . import attachments as att
from .config import QuartzConfig
from .errors import ToolError
from .notes import compose, parse
from .render import PngRenderer
from .site_assets import (
    AttachmentIndex,
    collect_references,
    png_sibling,
    site_path,
    slugify_path,
)
from .vault import ATTACHMENT_EXTENSIONS, Vault

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

# Where `npx quartz build` writes the site.
BUILD_OUTPUT_SUBDIR = "public"

# Attachments larger than this are reported and left as they are on the site.
# Far above the 2 MB the write tool accepts: the site already serves photos and
# GIFs of several MB, published by Quartz Syncer.
PUBLISH_MAX_ATTACHMENT_BYTES = 20_000_000


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
class AttachmentPlan:
    """What publish_site does to the site's images. Paths are vault-relative."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Referenced by a published note, but not in the vault.
    missing: list[dict] = field(default_factory=list)
    # In the vault, but not published: unsafe SVG, wrong bytes, too large.
    rejected: list[dict] = field(default_factory=list)
    # Unreferenced by our resolution, yet named in a published page's markdown,
    # so left in place rather than risk breaking it.
    kept: list[str] = field(default_factory=list)
    png_rendered: int = 0
    png_cached: int = 0
    png_failed: list[dict] = field(default_factory=list)
    # Site files expected after the build, content path -> built path.
    outputs: dict[str, str] = field(default_factory=dict)
    # Set after a local build: staged files the build did not emit.
    not_in_build: list[str] | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.updated or self.removed)

    def as_dict(self) -> dict:
        out: dict = {
            "added": sorted(self.added),
            "updated": sorted(self.updated),
            "removed": sorted(self.removed),
            "missing": sorted(self.missing, key=lambda m: (m["note"], m["reference"])),
            "total_changes": len(self.added) + len(self.updated) + len(self.removed),
            "png": {
                "rendered": self.png_rendered,
                "cached": self.png_cached,
                "failed": self.png_failed,
            },
        }
        if self.rejected:
            out["rejected"] = sorted(self.rejected, key=lambda r: r["path"])
        if self.kept:
            out["kept_unreferenced"] = sorted(self.kept)
        if self.not_in_build is not None:
            out["not_in_build"] = sorted(self.not_in_build)
        return out


@dataclass
class PublishPlan:
    added: list[str]
    updated: list[str]
    removed: list[str]
    # Notes we deliberately did not touch -- see DYNAMIC_BLOCK.
    skipped: list[str] = field(default_factory=list)
    attachments: AttachmentPlan = field(default_factory=AttachmentPlan)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.updated or self.removed) and self.attachments.is_empty

    def as_dict(self) -> dict:
        out = {
            "added": sorted(self.added),
            "updated": sorted(self.updated),
            "removed": sorted(self.removed),
            "total_changes": len(self.added) + len(self.updated) + len(self.removed),
            "attachments": self.attachments.as_dict(),
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

    def __init__(
        self,
        config: QuartzConfig,
        lock_dir: Path,
        renderer: PngRenderer | None = None,
    ) -> None:
        self._config = config
        self._lock_dir = lock_dir
        self._renderer = renderer or PngRenderer(lock_dir / "png-cache")

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
        # core.quotepath=off: by default git prints non-ASCII paths quoted and
        # octal-escaped ("content/G\303\266rlitz.md"), which never matched,
        # so every note with an accented name fell back to a byte comparison
        # and was rewritten on every publish.
        result = subprocess.run(
            ["git", "-c", "core.quotepath=off", "log", "--format=%ct",
             "--name-only", "--no-merges", "--", CONTENT_SUBDIR],
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
                # NFC, as the vault's paths are compared in that form.
                rel = unicodedata.normalize("NFC", line[len(CONTENT_SUBDIR) + 1 :])
                when.setdefault(rel, stamp)
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
            was = last_published.get(unicodedata.normalize("NFC", rel))
            if was is not None and mtime <= was + PUBLISH_GRACE_SECONDS:
                continue
            # NFC, git's form: a decomposed name from a macOS-synced vault
            # would otherwise be a second file beside the published one.
            target = self.content_dir / site_path(rel)
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
        # Compared in NFC: on macOS the vault walk can return decomposed names
        # while git's are composed, which removed every accented note.
        flagged = {site_path(rel) for rel in published}
        for existing in self.content_dir.rglob("*.md"):
            rel = existing.relative_to(self.content_dir).as_posix()
            if site_path(rel) not in flagged:
                existing.unlink()
                plan.removed.append(rel)

        return plan

    def stage_attachments(
        self, vault: Vault, published: dict[str, tuple[str, float]]
    ) -> AttachmentPlan:
        """Mirror the images published notes use, plus PNGs of their SVG heroes.

        Run after stage(), whose sweep has already dropped unpublished notes'
        markdown. Each referenced file lands at its vault-relative path under
        content/ (Quartz serves it at the slugified path, e.g.
        content/Attachments/Places/Vauban.svg -> /attachments/places/vauban.svg).
        Files are compared by SHA-256, so an unchanged image is never rewritten
        and git sees no change.

        Every published note counts, including ones stage() skipped for
        dataview blocks: those stay on the site, so their images must too.
        """
        plan = AttachmentPlan()
        index = AttachmentIndex(vault)

        # Site path (NFC, as git and the notes spell it) -> on-disk vault path.
        wanted: dict[str, str] = {}
        page_svgs: set[str] = set()
        for note_rel, (raw, _mtime) in published.items():
            for ref in collect_references(raw):
                hit = index.resolve(ref.target, source=note_rel)
                if hit is None:
                    plan.missing.append(
                        {"note": site_path(note_rel), "reference": ref.target}
                    )
                    continue
                rel = site_path(hit)
                wanted[rel] = hit
                if ref.is_page_image and rel.lower().endswith(".svg"):
                    page_svgs.add(rel)

        expected: set[str] = set(wanted)
        svg_bytes: dict[str, bytes] = {}
        for rel in sorted(wanted):
            data = self._load_attachment(vault, wanted[rel], rel, plan)
            if data is None:
                page_svgs.discard(rel)
                continue
            if rel in page_svgs:
                svg_bytes[rel] = data
            self._write_if_changed(rel, data, plan)

        # Link-preview PNGs for SVG page images. A PNG the vault itself holds
        # at that path, and that a note references, wins over a rendered one.
        to_render = {
            png_sibling(svg): data
            for svg, data in svg_bytes.items()
            if png_sibling(svg) not in wanted
        }
        result = self._renderer.render_all(to_render)
        plan.png_rendered, plan.png_cached = result.rendered, result.cached
        for png_rel, reason in sorted(result.failed.items()):
            plan.png_failed.append({"path": png_rel, "reason": reason})
        for png_rel, key in result.keys.items():
            expected.add(png_rel)
            self._write_if_changed(png_rel, self._renderer.read(key), plan)
        self._renderer.prune(set(result.keys.values()))

        # Rejected files stay as they are on the site: not updated, not removed.
        expected |= {r["path"] for r in plan.rejected}
        self._sweep_attachments(expected, plan)

        plan.outputs = {
            rel: f"{BUILD_OUTPUT_SUBDIR}/{slugify_path(rel)}"
            for rel in expected
            if (self.content_dir / rel).is_file()
        }
        return plan

    def _load_attachment(
        self, vault: Vault, disk_rel: str, rel: str, plan: AttachmentPlan
    ) -> bytes | None:
        """The vault file's bytes, or None (recorded as rejected) if unpublishable."""
        try:
            path = vault.resolve_any_attachment(disk_rel)
            data = vault.read_bytes(path, max_bytes=PUBLISH_MAX_ATTACHMENT_BYTES)
            # The site serves these from its own origin. An SVG with script in
            # it would be stored XSS, and resvg would follow an <image href> to
            # a local file -- so vault files get the same check as uploads.
            att.validate(data, path.suffix)
        except (ToolError, OSError) as exc:
            plan.rejected.append({"path": rel, "reason": str(exc)[:300]})
            return None
        return data

    def _write_if_changed(self, rel: str, data: bytes, plan: AttachmentPlan) -> None:
        target = self.content_dir / rel
        if target.is_file():
            if att.sha256(target.read_bytes()) == att.sha256(data):
                return
            plan.updated.append(rel)
        else:
            plan.added.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def _sweep_attachments(self, expected: set[str], plan: AttachmentPlan) -> None:
        """Remove site images that no published note uses any more.

        Conservative on purpose. Quartz Syncer publishes attachments into the
        same folder, and a page can name an image in ways this server does not
        parse (HTML <img>, a frontmatter key outside IMAGE_KEYS). So a file is
        removed only if no published page's markdown mentions its filename at
        all; otherwise it is kept and listed under kept_unreferenced.
        """
        wanted = {unicodedata.normalize("NFC", rel) for rel in expected}
        candidates = []
        for path in self.content_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ATTACHMENT_EXTENSIONS:
                continue
            rel = path.relative_to(self.content_dir).as_posix()
            if unicodedata.normalize("NFC", rel) not in wanted:
                candidates.append((rel, path))
        if not candidates:
            return

        corpus = unicodedata.normalize(
            "NFC",
            "\n".join(
                md.read_text(encoding="utf-8", errors="replace")
                for md in self.content_dir.rglob("*.md")
            ),
        ).lower()
        for rel, path in sorted(candidates):
            name = unicodedata.normalize("NFC", path.name).lower()
            spellings = {name, quote(name), name.replace(" ", "%20")}
            if any(s in corpus for s in spellings):
                plan.kept.append(rel)
                continue
            path.unlink()
            plan.removed.append(rel)

    def verify_build_outputs(self, plan: AttachmentPlan) -> None:
        """After a local build: which staged images the site did not emit."""
        if not self._config.build_locally:
            return
        root = self._config.repo_path
        plan.not_in_build = [
            rel for rel, built in plan.outputs.items() if not (root / built).is_file()
        ]

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

    def sync_with_remote(self) -> None:
        """Bring the working tree to the remote branch before staging.

        The site repo has other writers (Quartz Syncer in the Obsidian app, code
        PRs), so the remote moves between publishes. A push from a stale tree is
        rejected, and the commit it leaves behind wedges every later run: the
        next stage diffs against that commit and reports nothing to do. Nothing
        local is worth keeping -- stage() regenerates content/ from the vault on
        every run -- so reset to the remote rather than merging.
        """
        branch = self._config.branch
        fetch = self._git("fetch", "--no-tags", self._config.remote, branch)
        if fetch.returncode != 0:
            raise ToolError(
                "Could not fetch the site repo, so nothing was staged or pushed. "
                "Check the deploy key and network.\n\n" + fetch.stderr.strip()
            )
        reset = self._git("reset", "--hard", "FETCH_HEAD")
        if reset.returncode != 0:
            raise ToolError(f"git reset failed: {reset.stderr.strip()}")
        self._git("clean", "-fd", CONTENT_SUBDIR)

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
            # The commit exists locally; the next publish discards it when it
            # syncs with the remote, so a rejected push can simply be retried.
            raise ToolError(
                "The push failed, so the site is unchanged. If the remote moved "
                "meanwhile (rejected, non-fast-forward), publish again: each "
                "publish first resets to the remote. Otherwise check the deploy "
                "key and network.\n\n" + push.stderr.strip()
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
