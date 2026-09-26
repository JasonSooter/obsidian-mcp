"""Tests for the parts that must be right: confinement, links, appends, journal.

Run with:  .venv/bin/python -m pytest tests -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obsidian_mcp.errors import PathRejected
from obsidian_mcp.journal import Journal
from obsidian_mcp.links import LinkResolver, parse_links
from obsidian_mcp.notes import append_to_body, parse, set_frontmatter_key
from obsidian_mcp.quartz import is_published
from obsidian_mcp.vault import Vault, build_registry


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "MyVault"
    (root / "Projects").mkdir(parents=True)
    (root / "Daily").mkdir()
    (root / ".obsidian").mkdir()
    (root / "Projects" / "Media server.md").write_text(
        "---\ntags: [infra]\n---\n\n# Media server\n\nLinks to [[Anki]].\n"
    )
    (root / "Anki.md").write_text("# Anki\n")
    (root / ".obsidian" / "secrets.md").write_text("token: hunter2\n")
    return Vault(name="MyVault", root=root.resolve())


# --- confinement -----------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "../outside.md",
        "/etc/passwd",
        "Projects/../../escape.md",
        ".obsidian/secrets.md",
        "Projects/../.obsidian/secrets.md",
    ],
)
def test_traversal_and_excluded_dirs_are_rejected(vault: Vault, bad: str) -> None:
    with pytest.raises(PathRejected):
        vault.resolve(bad, must_exist=False)


def test_symlink_out_of_vault_is_rejected(vault: Vault, tmp_path: Path) -> None:
    """The check that a naive implementation gets wrong.

    The path contains no '..' and stays textually inside the vault; only
    resolving the symlink reveals that it leaves.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    (vault.root / "sneaky.md").symlink_to(outside)
    with pytest.raises(PathRejected):
        vault.resolve("sneaky.md", must_exist=True)


def test_symlinked_parent_directory_is_rejected(vault: Vault, tmp_path: Path) -> None:
    """A create through a symlinked parent must fail too -- must_exist=False
    resolves the parent precisely so this cannot slip through."""
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (vault.root / "linked").symlink_to(outside_dir)
    with pytest.raises(PathRejected):
        vault.resolve("linked/new.md", must_exist=False)


def test_extension_allowlist(vault: Vault) -> None:
    (vault.root / "script.sh").write_text("#!/bin/sh\n")
    with pytest.raises(PathRejected):
        vault.resolve("script.sh", must_exist=True)


def test_valid_path_resolves(vault: Vault) -> None:
    resolved = vault.resolve("Projects/Media server.md", must_exist=True)
    assert resolved.is_file()
    assert vault.relative(resolved) == "Projects/Media server.md"


def test_iter_notes_skips_excluded_dirs(vault: Vault) -> None:
    found = {vault.relative(p) for p in vault.iter_notes()}
    assert "Anki.md" in found
    assert not any(".obsidian" in f for f in found)


def test_atomic_write_leaves_no_temp_files(vault: Vault) -> None:
    target = vault.root / "new.md"
    vault.write_text_atomic(target, "hello\n")
    assert target.read_text() == "hello\n"
    assert not [p for p in vault.root.iterdir() if p.name.startswith(".obsidian-mcp.tmp")]


def test_registry_rejects_vault_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "vaults"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "escape").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        build_registry(root, ("escape",), "escape")


# --- links -----------------------------------------------------------------

def test_parse_link_forms() -> None:
    body = (
        "A [[Note]] and [[folder/Note]] and [[Note|alias]] and "
        "[[Note#Heading]] and ![[Embed]] and [[Note#^block]].\n"
    )
    refs = parse_links(body)
    assert [r.target for r in refs] == [
        "Note", "folder/Note", "Note", "Note", "Embed", "Note",
    ]
    assert refs[2].alias == "alias"
    assert refs[3].subpath == "#Heading"
    assert refs[4].is_embed is True
    assert refs[5].subpath == "#^block"


def test_links_inside_code_are_ignored() -> None:
    body = "Real [[One]]\n\n```\nNot [[Two]]\n```\n\nInline `[[Three]]` no.\n"
    assert [r.target for r in parse_links(body)] == ["One"]


def test_shortest_unique_path_resolution(tmp_path: Path) -> None:
    root = tmp_path / "v"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "Target.md").write_text("x")
    (root / "b" / "Target.md").write_text("x")
    (root / "Unique.md").write_text("x")
    (root / "a" / "Source.md").write_text("x")
    vault = Vault(name="v", root=root.resolve())
    resolver = LinkResolver(vault)

    # Unique basename anywhere.
    assert resolver.resolve("Unique", source="a/Source.md") == ("Unique.md", [])
    # Exact path beats everything.
    assert resolver.resolve("b/Target", source="a/Source.md")[0] == "b/Target.md"
    # Ambiguous: same folder as the source wins, alternatives reported.
    picked, alternatives = resolver.resolve("Target", source="a/Source.md")
    assert picked == "a/Target.md"
    assert set(alternatives) == {"a/Target.md", "b/Target.md"}
    # ...and the same link from the other folder resolves differently.
    assert resolver.resolve("Target", source="b/Source.md")[0] == "b/Target.md"
    # Broken link.
    assert resolver.resolve("Nope", source="a/Source.md") == (None, [])


# --- notes -----------------------------------------------------------------

def test_append_preserves_frontmatter() -> None:
    raw = "---\ntags: [a]\ntitle: T\n---\n\nBody.\n"
    out = append_to_body(raw, "Added.")
    note = parse(out)
    assert note.metadata == {"tags": ["a"], "title": "T"}
    assert note.body.strip().endswith("Added.")
    # Key order is preserved, so diffs stay small.
    assert out.index("tags:") < out.index("title:")


def test_append_under_heading_lands_in_the_right_section() -> None:
    raw = "# Note\n\n## Log\n\n- one\n\n## Other\n\n- keep\n"
    out = append_to_body(raw, "- two", heading="Log")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines.index("- two") < lines.index("## Other")
    assert out.endswith("- keep\n")


def test_append_under_deeper_subheading_stays_in_section() -> None:
    raw = "## Log\n\n### Sub\n\n- a\n\n## Next\n\n- b\n"
    out = append_to_body(raw, "- new", heading="Log")
    assert out.index("- new") < out.index("## Next")


def test_append_creates_missing_heading() -> None:
    out = append_to_body("# Note\n\nBody.\n", "captured", heading="Log")
    assert "## Log" in out
    assert out.strip().endswith("captured")


def test_malformed_frontmatter_still_parses() -> None:
    note = parse("---\nthis: [is: broken\n---\n\nBody\n")
    assert "Body" in note.body


def test_tags_from_frontmatter_and_inline() -> None:
    note = parse("---\ntags: [alpha]\n---\n\nText #beta and #nested/tag.\n\n# Heading\n")
    assert set(note.tags) == {"alpha", "beta", "nested/tag"}


def test_heading_is_not_a_tag() -> None:
    assert parse("# Title\n\nno tags here\n").tags == []


def test_publish_flag_roundtrip() -> None:
    raw = "---\ntitle: T\n---\n\nBody.\n"
    on = set_frontmatter_key(raw, "publish", True)
    assert is_published(on)
    off = set_frontmatter_key(on, "publish", None)
    assert not is_published(off)
    assert parse(off).metadata == {"title": "T"}


def test_publish_flag_on_note_without_frontmatter() -> None:
    on = set_frontmatter_key("Just a body.\n", "publish", True)
    assert is_published(on)
    assert "Just a body." in parse(on).body


# --- journal ---------------------------------------------------------------

@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "versions", tmp_path / "audit.log", retention_days=30)


def test_snapshot_and_restore(vault: Vault, journal: Journal) -> None:
    path = vault.resolve("Anki.md", must_exist=True)
    original = path.read_text()

    version_id = journal.snapshot(vault, path, op="edit")
    assert version_id is not None
    vault.write_text_atomic(path, "clobbered\n")

    versions = journal.list_versions(vault, "Anki.md", limit=10)
    assert [v["version_id"] for v in versions] == [version_id]
    assert versions[0]["op"] == "edit"
    assert journal.read_version(vault, "Anki.md", version_id) == original


def test_snapshot_of_missing_file_is_none(vault: Vault, journal: Journal) -> None:
    assert journal.snapshot(vault, vault.root / "nope.md", op="create") is None


def test_deleted_note_is_still_recoverable(vault: Vault, journal: Journal) -> None:
    path = vault.resolve("Anki.md", must_exist=True)
    original = path.read_text()
    version_id = journal.snapshot(vault, path, op="delete")
    path.unlink()
    assert journal.read_version(vault, "Anki.md", version_id) == original


@pytest.mark.parametrize("bad", ["../../../etc/passwd", "a/b", "..", "x\\y"])
def test_version_id_cannot_traverse(vault: Vault, journal: Journal, bad: str) -> None:
    """version_id is a second caller-supplied path component; it must be
    confined just as carefully as `path` itself."""
    with pytest.raises((ValueError, FileNotFoundError)):
        journal.read_version(vault, "Anki.md", bad)


def test_audit_log_appends(vault: Vault, journal: Journal, tmp_path: Path) -> None:
    journal.record(tool="edit_note", vault="MyVault", path="Anki.md")
    journal.record(tool="delete_note", vault="MyVault", path="Anki.md")
    lines = (tmp_path / "audit.log").read_text().strip().splitlines()
    assert len(lines) == 2
    assert '"tool": "delete_note"' in lines[1]


# --- vault_patch primitives -------------------------------------------------

from obsidian_mcp.notes import (  # noqa: E402
    PatchTargetMissing,
    find_block,
    find_heading_section,
    headings,
    patch,
    rewrite_link_targets,
)

DOC = """---
status: draft
---

# Note

## Log

- one

### Sub

- deep

## Other

- keep

A block line. ^blk-1
"""


def test_patch_heading_append_stays_in_section() -> None:
    out = patch(DOC, operation="append", target_type="heading", target="Log", content="- two")
    assert out.index("- two") < out.index("## Other")
    # The nested subsection is part of Log, so the text goes after it.
    assert out.index("- deep") < out.index("- two")


def test_patch_heading_prepend_goes_directly_under_heading() -> None:
    out = patch(DOC, operation="prepend", target_type="heading", target="Log", content="- first")
    assert out.index("- first") < out.index("- one")


def test_patch_heading_replace_keeps_the_heading() -> None:
    out = patch(DOC, operation="replace", target_type="heading", target="Log", content="- only")
    assert "## Log" in out
    assert "- one" not in out
    assert "- deep" not in out
    assert "- keep" in out          # a different section is untouched
    assert "## Other" in out


def test_patch_nested_heading_path() -> None:
    out = patch(DOC, operation="append", target_type="heading", target="Log::Sub", content="- nested")
    assert out.index("- nested") < out.index("## Other")
    assert out.index("- deep") < out.index("- nested")


def test_nested_heading_path_must_be_nested() -> None:
    """'Other::Sub' must not match the Sub that lives under Log."""
    with pytest.raises(PatchTargetMissing):
        patch(DOC, operation="append", target_type="heading", target="Other::Sub", content="x")


def test_patch_block_reference() -> None:
    out = patch(DOC, operation="append", target_type="block", target="^blk-1", content="after block")
    assert out.index("after block") > out.index("A block line.")


def test_patch_block_replace_keeps_the_block_id() -> None:
    """Dropping the ^id would silently break every link pointing at the block."""
    out = patch(DOC, operation="replace", target_type="block", target="blk-1", content="new text")
    assert "new text ^blk-1" in out


def test_patch_frontmatter_property() -> None:
    out = patch(DOC, operation="replace", target_type="frontmatter", target="status", content="done")
    assert parse(out).metadata["status"] == "done"
    assert "## Log" in out


def test_patch_missing_target_raises() -> None:
    with pytest.raises(PatchTargetMissing):
        patch(DOC, operation="append", target_type="heading", target="Nope", content="x")
    with pytest.raises(PatchTargetMissing):
        patch(DOC, operation="append", target_type="block", target="^nope", content="x")


def test_patch_rejects_bad_operation() -> None:
    with pytest.raises(ValueError):
        patch(DOC, operation="obliterate", target_type="heading", target="Log", content="x")


def test_headings_outline() -> None:
    outline = headings(DOC)
    assert [(h["level"], h["text"]) for h in outline] == [
        (1, "Note"), (2, "Log"), (3, "Sub"), (2, "Other"),
    ]


def test_find_helpers_report_positions() -> None:
    lines = DOC.splitlines()
    start, end, level = find_heading_section(lines, "Log")
    assert level == 2 and start < end
    assert find_block(lines, "blk-1") == lines.index("A block line. ^blk-1")


# --- link rewriting (vault_move) -------------------------------------------

def test_rewrite_preserves_alias_subpath_and_embed() -> None:
    body = "[[Old]] and [[Old|alias]] and [[Old#Head]] and ![[Old]] and [[Other]]\n"
    out, count = rewrite_link_targets(body, "Old", "New")
    assert count == 4
    assert "[[New]]" in out
    assert "[[New|alias]]" in out
    assert "[[New#Head]]" in out
    assert "![[New]]" in out
    assert "[[Other]]" in out


def test_rewrite_skips_links_inside_code() -> None:
    body = "Real [[Old]]\n\n```\nexample [[Old]]\n```\n"
    out, count = rewrite_link_targets(body, "Old", "New")
    assert count == 1
    assert "example [[Old]]" in out


# --- telemetry contract -----------------------------------------------------
#
# The Grafana dashboard queries these attributes by name as Loki structured
# metadata. A list or dict value flattens into an unqueryable string, and a
# renamed key silently empties a panel -- so both are pinned here.

import inspect  # noqa: E402
import logging  # noqa: E402

from obsidian_mcp import telemetry  # noqa: E402


def test_emit_stringifies_bools_and_drops_none(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="obsidian_mcp.telemetry"):
        telemetry.emit("tool call", tool="vault_read", mutation=False, vault=None, ok=True)
    record = caplog.records[-1]
    assert record.message == "tool call"
    # LogQL compares strings; a real bool would depend on backend rendering.
    assert record.mutation == "false"
    assert record.ok == "true"
    assert not hasattr(record, "vault")  # None is dropped, not sent as "None"


def test_journal_record_ships_only_scalars(vault: Vault, journal: Journal, caplog) -> None:
    """Lists are fine in the audit file but must not reach telemetry."""
    with caplog.at_level(logging.INFO, logger="obsidian_mcp.telemetry"):
        journal.record(
            tool="publish_site",
            vault="MyVault",
            path="<site>",
            added=["a.md", "b.md"],     # dropped
            added_count=2,              # kept -- this is what the panel reads
            commit_sha="abc123",
        )
    record = next(r for r in caplog.records if r.message == "vault mutation")
    assert record.added_count == 2
    assert record.commit_sha == "abc123"
    assert not hasattr(record, "added")


def test_instrumented_preserves_signature() -> None:
    """The MCP SDK builds each tool's JSON schema from the function signature.

    functools.wraps has to carry it through the decorator, or every tool's
    parameters silently vanish from the wire.
    """
    async def vault_read(path: str, vault: str | None = None) -> dict:
        """Docstring becomes the tool description."""
        return {}

    wrapped = telemetry.instrumented("vault_read")(vault_read)
    assert inspect.signature(wrapped) == inspect.signature(vault_read)
    assert wrapped.__doc__ == vault_read.__doc__
    assert wrapped.__name__ == "vault_read"


def test_instrumented_records_failure_and_reraises() -> None:
    import asyncio

    async def boom(vault: str | None = None) -> None:
        raise PathRejected("nope")

    wrapped = telemetry.instrumented("vault_read")(boom)
    with pytest.raises(PathRejected):
        asyncio.run(wrapped())


def test_emit_survives_reserved_logrecord_keys(caplog) -> None:
    """`created`, `module`, `filename` etc. are LogRecord fields.

    journal.record() forwards arbitrary per-tool fields into a log record, so a
    collision used to raise KeyError *out of the tool*. Enabling telemetry would
    have broken vault_append outright. Now they are renamed, and emit can never
    raise regardless.
    """
    with caplog.at_level(logging.INFO, logger="obsidian_mcp.telemetry"):
        telemetry.emit("vault mutation", created=True, module="x", path="Inbox/a.md")
    record = next(r for r in caplog.records if r.message == "vault mutation")
    assert record.created_attr == "true"
    assert record.module_attr == "x"
    assert record.path == "Inbox/a.md"   # not reserved, passes through


def test_emit_never_raises() -> None:
    """Observability must degrade to a missing panel, not a failed tool."""
    telemetry.emit("tool call", **{"weird key": object()})


# --- OAuth hardening --------------------------------------------------------
#
# All three came from a code review of anki-mcp, whose oauth package
# this one is ported from. Pinned here so the two copies cannot drift apart on
# the fixes the way they shared the bugs.

def test_client_ip_uses_the_last_forwarded_hop() -> None:
    """X-Forwarded-For is client-appendable.

    A caller who sends their own header, in front of a proxy that appends
    rather than replaces, leaves a forged value at the HEAD of the list. Keying
    the login rate limiter on that lets an attacker mint a fresh bucket per
    request by rotating it.
    """
    from obsidian_mcp.oauth.login import _client_ip

    class _Req:
        def __init__(self, xff=None, peer="9.9.9.9"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": peer})()

    assert _client_ip(_Req("evil, 1.2.3.4")) == "1.2.3.4"
    assert _client_ip(_Req("1.2.3.4")) == "1.2.3.4"
    assert _client_ip(_Req("a , b , 1.2.3.4 ")) == "1.2.3.4"
    assert _client_ip(_Req(None)) == "9.9.9.9"          # falls back to the peer
    assert _client_ip(_Req(" , , ")) == "9.9.9.9"       # all-empty is not an IP


def test_redirect_url_encodes_state_and_respects_fragments() -> None:
    """`state` is client-chosen, so it must be encoded, and the old
    `"?" in uri` test put parameters inside a fragment."""
    from urllib.parse import parse_qs, urlsplit
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    def build_redirect(redirect_uri: str, code: str, state: str | None) -> str:
        parts = urlsplit(redirect_uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.append(("code", code))
        if state:
            query.append(("state", state))
        return urlunsplit(parts._replace(query=urlencode(query)))

    nasty = 'a&b=c#frag "quoted"'
    url = build_redirect("https://claude.ai/cb?x=1", "CODE123", nasty)
    q = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert q["state"] == [nasty]          # round-trips exactly
    assert q["code"] == ["CODE123"]
    assert q["x"] == ["1"]                # pre-existing param survives
    assert "#frag" not in urlsplit(url).fragment

    # A redirect_uri with a fragment keeps it, and params land in the query.
    url = build_redirect("https://claude.ai/cb#section", "C", "s")
    parts = urlsplit(url)
    assert parts.fragment == "section"
    assert parse_qs(parts.query)["code"] == ["C"]


def test_malformed_totp_secret_raises_a_clear_config_error() -> None:
    """The concrete path that used to escape build() as a stack trace."""
    from obsidian_mcp.oauth.totp import InvalidTOTPSecret, normalise_secret

    with pytest.raises(InvalidTOTPSecret) as exc:
        normalise_secret("not-valid-base32!!!")
    assert "base32" in str(exc.value)
    # The spaced/lowercase/unpadded forms people actually paste still work.
    assert normalise_secret("jbsw y3dp ehpk 3pxp") == normalise_secret("JBSWY3DPEHPK3PXP")


# --- Path handling from code review ----------------------------------------

def test_resolve_dir_rejects_missing_folder_cleanly(vault: Vault) -> None:
    """Path.resolve() is strict=False by default, so a missing folder reaches
    the is_dir() check and becomes a PathRejected the model can act on, not an
    unhandled FileNotFoundError."""
    with pytest.raises(PathRejected) as exc:
        vault.resolve_dir("no/such/folder")
    assert "No folder at" in str(exc.value)
    assert vault.resolve_dir(None) == vault.root
    assert vault.resolve_dir("Projects").name == "Projects"


def test_excerpt_centres_on_regex_matches() -> None:
    """Escaping a regex query would look for its metacharacters literally, miss,
    and trim from the start of the line instead of around the hit."""
    from obsidian_mcp.search import excerpt

    line = "x" * 300 + " NEEDLE-42 " + "y" * 300
    # Regex query: must find NEEDLE-42 and centre on it.
    out = excerpt(line, r"NEEDLE-\d+", width=80, fixed_string=False)
    assert "NEEDLE-42" in out
    # Same query treated as literal finds nothing, so it falls back to the head.
    out_literal = excerpt(line, r"NEEDLE-\d+", width=80, fixed_string=True)
    assert "NEEDLE-42" not in out_literal
    # A fixed-string query still works.
    assert "NEEDLE-42" in excerpt(line, "NEEDLE-42", width=80, fixed_string=True)
    # An uncompilable pattern must degrade, not raise.
    excerpt(line, "unclosed(", width=80, fixed_string=False)


def test_relink_plan_ignores_same_named_notes_elsewhere(tmp_path: Path) -> None:
    """The bug Copilot found: vault_move unlinked the source before resolving,
    leaving only text matching, which repoints links that were never ours.

    b/Source.md's bare [[Target]] resolves to b/Target.md by proximity. Moving
    a/Target.md must not touch it.
    """
    from obsidian_mcp.tools.write import _plan_relink

    root = tmp_path / "v"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "Target.md").write_text("x")
    (root / "b" / "Target.md").write_text("x")
    (root / "b" / "Source.md").write_text("Refers to [[Target]].\n")
    (root / "a" / "Mine.md").write_text("Refers to [[Target]].\n")
    vault = Vault(name="v", root=root.resolve())

    class _Ctx:
        def read(self, v, p):
            return p.read_text()

    plan = _plan_relink(_Ctx(), vault, "a/Target.md")
    assert "a/Mine.md" in plan          # genuinely points at the moved note
    assert "b/Source.md" not in plan    # points at b/Target.md, must be left alone


def test_provider_builds_the_same_redirect_as_the_helper(tmp_path: Path) -> None:
    """The helper in the test above pins BEHAVIOUR, not this implementation —
    refactor the provider and it would not notice. This ties the two together.

    Adapted from the anki-mcp fix that closed the same gap in its copy.
    """
    from urllib.parse import parse_qs, urlsplit

    from obsidian_mcp.oauth.provider import ObsidianOAuthProvider
    from obsidian_mcp.oauth.store import Store

    provider = ObsidianOAuthProvider(Store(tmp_path / "oauth.db"))
    nasty = 'a&b=c#frag "quoted"'
    provider._store.put_pending(
        "L",
        {
            "client_id": "c1",
            "redirect_uri": "https://claude.ai/cb?x=1",
            "redirect_uri_provided_explicitly": True,
            "code_challenge": "ch",
            "state": nasty,
            "scopes": ["obsidian"],
            "resource": None,
        },
        600,
    )
    url = provider.complete_login("L")
    parts = urlsplit(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    assert q["state"] == [nasty]   # reserved characters survive intact
    assert q["x"] == ["1"]         # pre-existing query param preserved
    assert q["code"]
    assert "#frag" not in parts.fragment


def test_malformed_totp_secret_exits_cleanly(tmp_path: Path, monkeypatch) -> None:
    """The bug was never that normalise_secret raises — it is that the exception
    escaped build() as a stack trace, so the operator got no exit code and the
    finally block never flushed telemetry. Assert on main()'s return value,
    which is the behaviour that actually broke.

    Supersedes the weaker version that only checked the exception type.
    """
    (tmp_path / "vaults" / "v").mkdir(parents=True)
    (tmp_path / "state").mkdir()

    for key, value in {
        "OBSIDIAN_VAULT_ROOT": str(tmp_path / "vaults"),
        "OBSIDIAN_VAULTS": "v",
        "OBSIDIAN_MCP_STATE_DIR": str(tmp_path / "state"),
        "OBSIDIAN_MCP_LOGIN_PASSWORD": "correct-horse-battery-staple",
        "OBSIDIAN_MCP_TOTP_SECRET": "!!!not-base32!!!",
        "OBSIDIAN_MCP_PORT": "8999",
        "QUARTZ_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    from obsidian_mcp.__main__ import main

    # Exits 2 rather than raising, and never reaches uvicorn.
    assert main() == 2


# --- Quartz frontmatter rendering -------------------------------------------
#
# content/ is also written by the Quartz Syncer Obsidian plugin. If what we emit
# differs from what it emits, the two publishers rewrite each other's files
# indefinitely. These pin the shape derived from 42 already-published notes.

def test_render_for_publish_matches_the_syncer_shape(tmp_path: Path) -> None:
    from obsidian_mcp.quartz import render_for_publish

    raw = (
        "---\n"
        "title: Idaho Stop\n"
        "date created: 2024-06-04T19:23:37-06:00\n"
        "date modified: 2026-02-21T09:56:46-07:00\n"
        "publish: true\n"
        "---\n\n"
        "# Idaho Stop\n\nBody text.\n"
    )
    out = render_for_publish(raw, 1784533754.0, tmp_path / "absent.md")   # 2026-07-20T07:49:14Z
    keys = [l.split(":")[0] for l in out.split("---")[1].strip().splitlines() if ":" in l]

    # Key order is part of the contract, not incidental.
    assert keys[:5] == ["publish", "title", "created", "modified", "published"]
    assert "date created" in keys and "date modified" in keys
    assert "modified: 2026-07-20T07:49:14.000Z" in out
    assert "published: 2026-07-20T07:49:14.000Z" in out
    # Original datetime values survive byte-exact: unconverted and unquoted.
    assert "date created: 2024-06-04T19:23:37-06:00" in out
    # published mirrors modified, as it does in all 42 published notes.
    body = out.split("---")[2]
    assert "# Idaho Stop" in body


def test_created_is_preserved_once_published(tmp_path: Path) -> None:
    """Birthtime does not survive an rclone copy onto the server, so `created`
    is taken from the already-published copy and never regenerated. Without
    this, every publish would rewrite every creation date."""
    from obsidian_mcp.quartz import render_for_publish

    site = tmp_path / "Idaho Stop.md"
    site.write_text(
        "---\npublish: true\ntitle: Idaho Stop\ncreated: 2024-06-05T01:23:37.766Z\n"
        "modified: 2026-01-01T00:00:00.000Z\npublished: 2026-01-01T00:00:00.000Z\n---\n\nOld.\n"
    )
    raw = "---\ntitle: Idaho Stop\npublish: true\n---\n\nNew body.\n"
    out = render_for_publish(raw, 1784533754.0, site)

    assert "created: 2024-06-05T01:23:37.766Z" in out   # preserved, not regenerated
    assert "modified: 2026-07-20T07:49:14.000Z" in out  # but modified did move
    assert "New body." in out


def test_rendering_is_stable_across_runs(tmp_path: Path) -> None:
    """Re-publishing an unchanged note must produce identical bytes, or every
    run reports every note as updated and fights the other publisher."""
    from obsidian_mcp.quartz import render_for_publish

    site = tmp_path / "n.md"
    raw = "---\ntitle: N\npublish: true\ndate created: 2024-01-01\n---\n\nBody.\n"
    first = render_for_publish(raw, 1784533754.0, site)
    site.write_text(first)
    second = render_for_publish(raw, 1784533754.0, site)
    assert first == second


def test_datetime_frontmatter_round_trips_byte_exact() -> None:
    """PyYAML resolves date-like scalars to datetimes and re-emits them in its
    own format, turning `2024-06-04T19:23:37-06:00` into
    `2024-06-04 19:23:37-06:00`. That silently rewrote the reader's frontmatter
    on every vault_write/vault_patch, and would have reformatted the dates of
    every published note on the site."""
    for line in (
        "date created: 2024-06-04T19:23:37-06:00",
        "date modified: 2026-02-21T09:56:46-07:00",
        "date: 2024-01-15",
        "modified: 2026-08-07T04:46:38.288Z",
    ):
        from obsidian_mcp.notes import compose as _compose

        raw = f"---\ntitle: N\n{line}\naliases:\n  - A\n---\n\nBody.\n"
        note = parse(raw)
        out = _compose(note.metadata, note.body)
        assert line in out, f"{line!r} was rewritten as {out!r}"


def test_frontmatter_formatting_matches_obsidian_conventions() -> None:
    """Obsidian indents block sequences under their key and leaves an empty
    value empty. PyYAML does neither by default, so without this every write
    would rewrite the whole frontmatter block -- and on the Quartz site, put
    our publisher in a rewrite loop with the Syncer plugin."""
    from obsidian_mcp.notes import compose as _compose

    raw = (
        "---\ntitle: N\naliases:\ntags:\n  - places\n  - travel\n"
        "date created: 2023-09-26T12:52:39+02:00\ncategories:\n  - Places\n---\n\nBody.\n"
    )
    note = parse(raw)
    out = _compose(note.metadata, note.body)
    assert raw.split("---")[1] == out.split("---")[1], "frontmatter was reformatted"
    assert "aliases:\n" in out and "aliases: null" not in out
    assert "tags:\n  - places" in out


def test_notes_with_dynamic_blocks_are_skipped_not_clobbered() -> None:
    """Dataview/query blocks are executed by an Obsidian plugin at publish time.
    This server has no plugin runtime, so publishing such a note would replace
    the generated content with the raw query -- visible breakage on the live
    page. They are skipped and left as the plugin last published them."""
    from obsidian_mcp.quartz import has_dynamic_blocks

    for body in (
        "# N\n\n```dataview\ntask\n```\n",
        "# N\n\n```dataviewjs\ndv.pages()\n```\n",
        "# N\n\n  ```datacore\nfoo\n```\n",
        "# N\n\n```DATAVIEW\ntask\n```\n",       # case-insensitive
    ):
        assert has_dynamic_blocks(body), f"missed: {body!r}"

    for body in (
        # ```query is Obsidian's core search block; the plugin passes it
        # through verbatim, so we can publish those notes normally.
        "# N\n\n```query\ntag:#x\n```\n",
        "# N\n\nPlain prose about dataview as a topic.\n",
        "# N\n\n```python\nprint('dataview')\n```\n",   # a normal code block
        "# N\n\nInline `dataview` mention.\n",
    ):
        assert not has_dynamic_blocks(body), f"false positive: {body!r}"


def test_unchanged_notes_keep_their_published_timestamps(tmp_path: Path) -> None:
    """The server's mtimes are whole seconds (rclone truncates) while the plugin
    writes milliseconds. Without preserving them, every note would report as
    updated forever over a sub-second difference that means nothing -- and
    `modified` would move on notes that did not change."""
    from obsidian_mcp.quartz import render_for_publish

    site = tmp_path / "n.md"
    site.write_text(
        "---\npublish: true\ntitle: N\ncreated: 2024-01-01T00:00:00.000Z\n"
        "modified: 2026-08-07T04:46:38.288Z\npublished: 2026-08-07T04:46:38.288Z\n"
        "---\n\nBody.\n"
    )
    raw = "---\ntitle: N\npublish: true\n---\n\nBody.\n"
    # Same second, different milliseconds from what the site records.
    out = render_for_publish(raw, 1786106798.0, site)
    assert out == site.read_text(), "identical content should not be rewritten"

    # A real body change does update, and moves the timestamps.
    changed = render_for_publish(
        "---\ntitle: N\npublish: true\n---\n\nDifferent body.\n", 1786106798.0, site
    )
    assert "Different body." in changed
    assert "modified: 2026-08-07T04:46:38.288Z" not in changed


def test_markdown_normalisation_matches_the_plugin_serializer() -> None:
    """The Syncer plugin serialises notes through mdast-util-to-markdown with
    emphasis "_" and bullet "-". We match those two settings; the rest of that
    serializer (escaping especially) is deliberately not reproduced.

    The negative cases matter more than the positive ones: an asterisk in code
    is content, and rewriting it would corrupt the published note."""
    from obsidian_mcp.quartz import normalise_markdown as n

    assert n("***Goal***") == "_**Goal**_"
    assert n("*italic*") == "_italic_"
    assert n("**bold**") == "**bold**"          # strong stays asterisks
    assert n("* item\n+ other") == "- item\n- other"

    # Must not touch:
    assert n("a `*not emphasis*` b") == "a `*not emphasis*` b"
    assert n("```\n*code star*\n```") == "```\n*code star*\n```"
    assert n("2 * 3 * 4") == "2 * 3 * 4"        # bare multiplication
    assert n("snake_case_word") == "snake_case_word"
    assert n("- [ ] task *x*") == "- [ ] task _x_"


def test_iso_milliseconds_come_from_the_datetime() -> None:
    """`when % 1` cannot represent most fractions exactly in binary floating
    point: 1786106798.001 truncated to `.000Z`. Deriving both halves from the
    same datetime also guarantees they cannot disagree."""
    from datetime import datetime, timezone

    from obsidian_mcp.quartz import _iso

    for ts in (1786106798.288, 1786106798.001, 1786106798.999, 1786106798.0):
        moment = datetime.fromtimestamp(ts, tz=timezone.utc)
        expected = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
        assert _iso(ts) == expected, f"{ts}: {_iso(ts)} != {expected}"


def test_indented_code_fences_are_masked() -> None:
    """CommonMark allows a fence indented up to three spaces. Anchoring the
    mask at column 0 left those unmasked, so emphasis and bullets inside them
    were rewritten -- corrupting a published note, which is the exact failure
    the masking exists to prevent."""
    from obsidian_mcp.quartz import normalise_markdown as n

    for block in (
        "   ```\n   *not emphasis*\n   * not a bullet\n   ```\n",
        "  ~~~\n  *not emphasis*\n  ~~~\n",
        " ````\n *four backticks*\n ````\n",
        "```\n*flush left*\n```\n",
    ):
        assert n(block) == block, f"corrupted: {block!r}"

    # Real emphasis outside a fence still converts.
    assert n("*x*\n\n   ```\n   *y*\n   ```\n") == "_x_\n\n   ```\n   *y*\n   ```\n"


def test_skipped_reason_matches_what_is_skipped() -> None:
    """The message named ```query blocks while DYNAMIC_BLOCK deliberately
    excludes them -- a note with one publishes normally, so saying otherwise
    sends the reader looking for a problem that is not there."""
    from obsidian_mcp.quartz import PublishPlan, has_dynamic_blocks

    assert not has_dynamic_blocks("```query\ntag:#x\n```")
    plan = PublishPlan(added=[], updated=[], removed=[], skipped=["a.md"])
    reason = plan.as_dict()["skipped_reason"]
    assert "dataview or datacore" in reason
    assert "NOT skipped" in reason


def test_mtime_ns_survives_a_json_double_round_trip() -> None:
    """Nanosecond mtimes are ~1.8e18 -- 199x above the largest integer a double
    holds exactly. A client parsing JSON numbers as doubles rounds the value by
    a few hundred nanoseconds and sends it back changed, so an equality check
    failed every time and the concurrency guard was unusable from a real
    client. Returning a string fixes the round trip; the tolerance covers
    clients that already rounded it.
    """
    from obsidian_mcp.tools.write import MTIME_TOLERANCE_NS

    real = 1788422112937306177          # the value observed in the wild
    assert real > 2**53, "premise: too large for exact double representation"

    rounded = int(float(real))          # what a double-parsing client returns
    assert rounded != real, "premise: the round trip must actually lose precision"
    assert abs(real - rounded) <= MTIME_TOLERANCE_NS, "tolerance must absorb it"

    # A string round-trips exactly, whatever the client's number handling.
    assert int(str(real)) == real

    # The tolerance must NOT absorb a real edit. Filesystem mtimes move by
    # milliseconds at minimum when a file is actually rewritten, which leaves
    # two orders of magnitude between the widest rounding error we tolerate and
    # the smallest genuine change.
    one_millisecond = 1_000_000
    assert MTIME_TOLERANCE_NS * 100 <= one_millisecond


def test_expected_mtime_accepts_the_forms_a_client_actually_sends() -> None:
    """A client that parsed mtime_ns as a JSON number serialises it back as
    `1.788422843329067e+18`, not as digits. Rejecting that would defeat the
    tolerance, which exists for exactly those clients."""
    from obsidian_mcp.errors import ToolError
    from obsidian_mcp.tools.write import MTIME_TOLERANCE_NS, _parse_mtime

    real = 1788422843329066594
    assert _parse_mtime(str(real)) == real       # the string we hand out
    assert _parse_mtime(real) == real            # a native int
    assert _parse_mtime(" 123 ") == 123          # whitespace

    # Scientific notation and a trailing .0 both parse, and land close enough
    # for the guard to accept them as the same moment.
    for text in (f"{float(real):.17g}", "1.788422843329067e+18", "123.0"):
        parsed = _parse_mtime(text)
        assert isinstance(parsed, int)
    assert abs(_parse_mtime(f"{float(real):.16g}") - real) <= MTIME_TOLERANCE_NS

    # Genuinely unparseable input is still an error naming what is wanted.
    for bad in ("abc", "", "NaN", "0x1F"):
        with pytest.raises(ToolError) as exc:
            _parse_mtime(bad)
        assert "integer nanosecond" in str(exc.value)


# --- Standalone defaults ----------------------------------------------------

def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in {
        "OBSIDIAN_VAULT_ROOT": str(tmp_path / "vaults"),
        "OBSIDIAN_MCP_STATE_DIR": str(tmp_path / "state"),
        "OBSIDIAN_MCP_LOGIN_PASSWORD": "correct-horse-battery-staple",
        "OBSIDIAN_MCP_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("OBSIDIAN_VAULTS", "OBSIDIAN_DEFAULT_VAULT", "QUARTZ_ENABLED"):
        monkeypatch.delenv(key, raising=False)


def test_vault_list_is_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No default vault name: a guess would point at a folder that isn't there."""
    from obsidian_mcp.config import ConfigError, load

    _base_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="OBSIDIAN_VAULTS must be set"):
        load()


def test_quartz_publishing_is_off_unless_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Publishing needs a Quartz checkout most installs don't have."""
    from obsidian_mcp.config import load

    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("OBSIDIAN_VAULTS", "MyVault")
    assert load().quartz is None
    monkeypatch.setenv("QUARTZ_ENABLED", "true")
    assert load().quartz is not None
