"""Attachment tools and attachment publishing.

The tools are exercised through a real MCPServer, so these cover the registered
call path (argument binding, ToolError -> is_error) and not just the helpers.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as SDKToolError

from obsidian_mcp import attachments as att
from obsidian_mcp.config import Config, QuartzConfig
from obsidian_mcp.journal import Journal
from obsidian_mcp.quartz import Publisher
from obsidian_mcp.render import PngRenderer
from obsidian_mcp.site_assets import (
    AttachmentIndex,
    collect_references,
    slugify_path,
)
from obsidian_mcp.tools import attachments as attachment_tools
from obsidian_mcp.tools import history, write
from obsidian_mcp.tools._common import Context
from obsidian_mcp.vault import Vault, VaultRegistry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" '
    b'viewBox="0 0 1200 630"><defs><linearGradient id="a"><stop offset="0" '
    b'stop-color="#88c0d0"/></linearGradient></defs>'
    b'<rect width="1200" height="630" fill="url(#a)"/>'
    b'<use href="#a"/><text x="10" y="50" font-family="\'Space Grotesk\'">Vauban</text></svg>'
)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# --- harness ----------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "MyVault"
    (root / "Attachments" / "Places").mkdir(parents=True)
    (root / "Places").mkdir()
    (root / "Notes").mkdir()
    return Vault(name="MyVault", root=root.resolve())


def _config(tmp_path: Path, vault: Vault, **overrides) -> Config:
    fields = dict(
        vault_root=vault.root.parent,
        vault_names=(vault.name,),
        default_vault=vault.name,
        state_dir=tmp_path / "state",
        login_password="x" * 12,
        totp_secret="JBSWY3DPEHPK3PXP",
        host="127.0.0.1",
        port=0,
        public_url="http://localhost",
        max_file_bytes=2_000_000,
        journal_retention_days=30,
        environment="test",
        quartz=None,
    )
    fields.update(overrides)
    return Config(**fields)


@pytest.fixture
def server(tmp_path: Path, vault: Vault):
    config = _config(tmp_path, vault, max_attachment_bytes=4096)
    journal = Journal(
        config.versions_dir, config.audit_log_path, retention_days=30
    )
    ctx = Context(
        config=config,
        vaults=VaultRegistry({vault.name: vault}, vault.name),
        journal=journal,
        publisher=None,
    )
    mcp = MCPServer(name="test")
    write.register(mcp, ctx)
    attachment_tools.register(mcp, ctx)
    history.register(mcp, ctx)
    return mcp


class ToolFailed(Exception):
    pass


def call(mcp: MCPServer, name: str, **args) -> dict:
    try:
        result = asyncio.run(mcp.call_tool(name, args))
    except SDKToolError as exc:
        # The message the client would see, with its cause if wrapped.
        raise ToolFailed(f"{exc} {exc.__cause__ or ''}") from exc
    text = result.content[0].text if result.content else ""
    if result.is_error:
        raise ToolFailed(text)
    return json.loads(text)


# --- vault_write_attachment ---------------------------------------------------


@pytest.mark.parametrize(
    "name,data",
    [("a.png", PNG), ("a.jpg", JPEG), ("a.jpeg", JPEG), ("a.gif", GIF),
     ("a.webp", WEBP), ("a.svg", SVG)],
)
def test_write_attachment_accepts_each_allowed_type(server, vault, name, data) -> None:
    out = call(server, "vault_write_attachment",
               path=f"Attachments/{name}", content_base64=b64(data))
    assert out["bytes"] == len(data)
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert isinstance(out["mtime_ns"], str) and out["mtime_ns"].isdigit()
    assert (vault.root / "Attachments" / name).read_bytes() == data


def test_write_attachment_creates_parent_folders_atomically(server, vault) -> None:
    call(server, "vault_write_attachment",
         path="Attachments/Places/New/Deep.svg", content_base64=b64(SVG))
    folder = vault.root / "Attachments" / "Places" / "New"
    assert (folder / "Deep.svg").read_bytes() == SVG
    assert [p.name for p in folder.iterdir()] == ["Deep.svg"]  # no temp file left


@pytest.mark.parametrize(
    "name,data",
    [("fake.png", JPEG), ("fake.jpg", PNG), ("fake.gif", b"<html></html>"),
     ("fake.webp", b"RIFF\x00\x00\x00\x00AVI "), ("fake.svg", PNG),
     ("html.png", b"<script>alert(1)</script>")],
)
def test_magic_byte_mismatch_is_rejected(server, vault, name, data) -> None:
    with pytest.raises(ToolFailed, match="signature|well-formed|UTF-8"):
        call(server, "vault_write_attachment",
             path=f"Attachments/{name}", content_base64=b64(data))
    assert not (vault.root / "Attachments" / name).exists()


@pytest.mark.parametrize(
    "svg,why",
    [
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "script"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><s:script xmlns:s="http://www.w3.org/2000/svg"/></svg>', "script"),
        (b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', "event handler"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><rect ONCLICK="x()"/></svg>', "event handler"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><rect/></a></svg>', "javascript"),
        (b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="https://evil.example/x.png"/></svg>', "outside the file"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><image href="/etc/passwd"/></svg>', "outside the file"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://x.example/p)"/></svg>', "external"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(x.css);</style></svg>', "external"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div xmlns="http://www.w3.org/1999/xhtml"/></foreignObject></svg>', "foreignObject"),
        (b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>', "DOCTYPE"),
        (b'<html xmlns="http://www.w3.org/1999/xhtml"/>', "root element"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>', "well-formed"),
    ],
)
def test_unsafe_svg_is_rejected(server, vault, svg, why) -> None:
    with pytest.raises(ToolFailed, match=why):
        call(server, "vault_write_attachment",
             path="Attachments/bad.svg", content_base64=b64(svg))
    assert not (vault.root / "Attachments" / "bad.svg").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../outside.png",
        "Attachments/../../outside.png",
        "/etc/x.png",
        "Places/Vauban.png",          # not an attachment folder
        "Attachmentsx/a.png",         # prefix is not a folder match
        "Attachments",                # the folder itself
        "Attachments/note.md",        # wrong type for this tool
        "Attachments/x.exe",
        ".obsidian/x.png",
        "Attachments/.trash/x.png",
    ],
)
def test_paths_outside_the_allow_list_are_rejected(server, path) -> None:
    with pytest.raises(ToolFailed):
        call(server, "vault_write_attachment", path=path, content_base64=b64(PNG))


def test_symlink_out_of_the_vault_is_rejected(server, vault, tmp_path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (vault.root / "Attachments" / "linked").symlink_to(outside)
    with pytest.raises(ToolFailed, match="outside the vault"):
        call(server, "vault_write_attachment",
             path="Attachments/linked/x.png", content_base64=b64(PNG))
    assert not (outside / "x.png").exists()


def test_case_only_twins_are_rejected(server, vault) -> None:
    """Linux allows `places/` beside `Places/`; Dropbox on macOS does not."""
    with pytest.raises(ToolFailed, match="only in case"):
        call(server, "vault_write_attachment",
             path="Attachments/places/x.svg", content_base64=b64(SVG))
    call(server, "vault_write_attachment",
         path="Attachments/Places/Vauban.svg", content_base64=b64(SVG))
    with pytest.raises(ToolFailed, match="only in case"):
        call(server, "vault_write_attachment", mode="overwrite",
             path="Attachments/Places/vauban.svg", content_base64=b64(SVG))


def test_size_cap(server) -> None:
    big = PNG + b"\x00" * 5000  # cap is 4096 in this fixture
    with pytest.raises(ToolFailed, match="cap"):
        call(server, "vault_write_attachment",
             path="Attachments/big.png", content_base64=b64(big))


def test_bad_base64_is_rejected(server) -> None:
    with pytest.raises(ToolFailed, match="base64"):
        call(server, "vault_write_attachment",
             path="Attachments/a.png", content_base64="not base64!!")


def test_data_url_prefix_is_accepted(server, vault) -> None:
    call(server, "vault_write_attachment", path="Attachments/a.svg",
         content_base64="data:image/svg+xml;base64," + b64(SVG))
    assert (vault.root / "Attachments" / "a.svg").read_bytes() == SVG


def test_create_refuses_an_existing_file_and_overwrite_replaces_it(server, vault) -> None:
    call(server, "vault_write_attachment", path="Attachments/a.png", content_base64=b64(PNG))
    with pytest.raises(ToolFailed, match="already exists"):
        call(server, "vault_write_attachment", path="Attachments/a.png",
             content_base64=b64(PNG + b"1"))
    out = call(server, "vault_write_attachment", path="Attachments/a.png",
               content_base64=b64(PNG + b"2"), mode="overwrite")
    assert out["created"] is False
    assert (vault.root / "Attachments" / "a.png").read_bytes() == PNG + b"2"


def test_expected_mtime_ns_guards_an_overwrite(server, vault) -> None:
    first = call(server, "vault_write_attachment", path="Attachments/a.png",
                 content_base64=b64(PNG))
    # The string from the write (or a read) is accepted...
    second = call(server, "vault_write_attachment", path="Attachments/a.png",
                  content_base64=b64(PNG + b"2"), mode="overwrite",
                  expected_mtime_ns=first["mtime_ns"])
    # ...and a stale one fails without touching the file.
    stale = str(int(second["mtime_ns"]) - 5_000_000_000)
    with pytest.raises(ToolFailed, match="changed since you read it"):
        call(server, "vault_write_attachment", path="Attachments/a.png",
             content_base64=b64(PNG + b"3"), mode="overwrite",
             expected_mtime_ns=stale)
    assert (vault.root / "Attachments" / "a.png").read_bytes() == PNG + b"2"


def test_expected_mtime_on_a_missing_file_fails(server) -> None:
    with pytest.raises(ToolFailed, match="deleted since you read it"):
        call(server, "vault_write_attachment", path="Attachments/a.png",
             content_base64=b64(PNG), mode="overwrite", expected_mtime_ns="1")


def test_overwrite_snapshots_and_restore_version_undoes_it(server, vault) -> None:
    call(server, "vault_write_attachment", path="Attachments/a.png", content_base64=b64(PNG))
    out = call(server, "vault_write_attachment", path="Attachments/a.png",
               content_base64=b64(PNG + b"new"), mode="overwrite")
    assert out["previous_version_id"]

    versions = call(server, "list_versions", path="Attachments/a.png")
    assert versions["count"] == 1
    version_id = versions["versions"][0]["version_id"]
    assert version_id.endswith(".png")

    call(server, "restore_version", path="Attachments/a.png", version_id=version_id)
    # Byte-exact: a text round trip would mangle the PNG.
    assert (vault.root / "Attachments" / "a.png").read_bytes() == PNG


# --- read / list / delete -------------------------------------------------


def test_read_attachment_metadata_and_optional_content(server) -> None:
    call(server, "vault_write_attachment", path="Attachments/a.svg", content_base64=b64(SVG))
    meta = call(server, "vault_read_attachment", path="Attachments/a.svg")
    assert meta["content_type"] == "image/svg+xml"
    assert meta["sha256"] == hashlib.sha256(SVG).hexdigest()
    assert "content_base64" not in meta
    full = call(server, "vault_read_attachment", path="Attachments/a.svg", include_content=True)
    assert base64.b64decode(full["content_base64"]) == SVG
    # The read's mtime_ns is accepted by an overwrite.
    call(server, "vault_write_attachment", path="Attachments/a.svg",
         content_base64=b64(SVG), mode="overwrite", expected_mtime_ns=meta["mtime_ns"])


def test_list_attachments(server, vault) -> None:
    call(server, "vault_write_attachment", path="Attachments/Places/B.svg", content_base64=b64(SVG))
    call(server, "vault_write_attachment", path="Attachments/a.png", content_base64=b64(PNG))
    (vault.root / "Places" / "stray.png").write_bytes(PNG)  # outside the allow-list

    everything = call(server, "vault_list_attachments", sort="path")
    assert [a["path"] for a in everything["attachments"]] == [
        "Attachments/Places/B.svg", "Attachments/a.png"]
    places = call(server, "vault_list_attachments", folder="Attachments/Places")
    assert places["total"] == 1 and places["attachments"][0]["size"] == len(SVG)
    with pytest.raises(ToolFailed):
        call(server, "vault_list_attachments", folder="Places")


def test_delete_moves_an_attachment_to_trash_and_is_restorable(server, vault) -> None:
    call(server, "vault_write_attachment", path="Attachments/a.png", content_base64=b64(PNG))
    out = call(server, "vault_delete", path="Attachments/a.png")
    assert not (vault.root / "Attachments" / "a.png").exists()
    assert (vault.root / out["trashed_to"]).read_bytes() == PNG
    call(server, "restore_version", path="Attachments/a.png",
         version_id=out["previous_version_id"])
    assert (vault.root / "Attachments" / "a.png").read_bytes() == PNG


def test_delete_refuses_attachments_outside_the_allow_list(server, vault) -> None:
    (vault.root / "Places" / "stray.png").write_bytes(PNG)
    with pytest.raises(ToolFailed):
        call(server, "vault_delete", path="Places/stray.png")
    assert (vault.root / "Places" / "stray.png").exists()


def test_tool_description_states_the_contract(server) -> None:
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    text = tools["vault_write_attachment"].description
    for phrase in ("base64", "Attachments/", "create", "overwrite", "STRING", ".svg"):
        assert phrase in text


# --- reference collection ----------------------------------------------------


def test_collect_references_finds_every_form() -> None:
    raw = (
        "---\nimage: Attachments/Places/Vauban.svg\ncover: \"[[Cover.jpg]]\"\n"
        "banner: https://example.com/remote.png\nsocialImage: social.png\n"
        "publish: true\n---\n\n# Vauban\n\n"
        "![[Map.png]] and ![[Sized.webp|300]] and ![[Anchored.gif#x]]\n"
        "![alt](../Attachments/Rel%20Path.png) ![t](<Spaced Name.jpg> \"title\")\n"
        "![remote](https://example.com/x.png) [[Not An Embed.png]] ![[Note]]\n"
        "`![[InCode.png]]`\n```\n![[InFence.png]]\n```\n"
    )
    refs = collect_references(raw)
    assert [(r.target, r.is_page_image) for r in refs] == [
        ("Attachments/Places/Vauban.svg", True),
        ("Cover.jpg", True),
        ("social.png", True),
        ("Map.png", False),
        ("Sized.webp", False),
        ("Anchored.gif", False),
        ("../Attachments/Rel Path.png", False),
        ("Spaced Name.jpg", False),
    ]


def test_attachment_resolution_follows_obsidian(vault) -> None:
    (vault.root / "Attachments" / "Places" / "Vauban.svg").write_bytes(SVG)
    (vault.root / "Attachments" / "Dup.png").write_bytes(PNG)
    (vault.root / "Notes" / "Sub").mkdir()
    (vault.root / "Notes" / "Sub" / "Dup.png").write_bytes(PNG)
    (vault.root / "Attachments" / "Rel Path.png").write_bytes(PNG)
    index = AttachmentIndex(vault)
    assert index.resolve("Attachments/Places/Vauban.svg", source="Places/V.md") == "Attachments/Places/Vauban.svg"
    assert index.resolve("attachments/places/vauban.svg", source="Places/V.md") == "Attachments/Places/Vauban.svg"
    assert index.resolve("Vauban.svg", source="Places/V.md") == "Attachments/Places/Vauban.svg"
    assert index.resolve("../Attachments/Rel Path.png", source="Places/V.md") == "Attachments/Rel Path.png"
    # Ambiguous basename: same folder as the note wins, else fewest segments.
    assert index.resolve("Dup.png", source="Notes/Sub/N.md") == "Notes/Sub/Dup.png"
    assert index.resolve("Dup.png", source="Places/V.md") == "Attachments/Dup.png"
    assert index.resolve("Nope.png", source="Places/V.md") is None


def test_slugify_matches_quartz() -> None:
    assert slugify_path("Attachments/Places/Vauban.svg") == "attachments/places/vauban.svg"
    assert slugify_path("Attachments/Alfred Hero.jpg") == "attachments/alfred-hero.jpg"
    assert slugify_path("Attachments/A & B #1?.png") == "attachments/a--and--b-1.png"
    assert slugify_path("Attachments/Places/Places.svg") == "attachments/places/index.svg"


# --- publish: stage_attachments -------------------------------------------------


class CountingRenderer(PngRenderer):
    """The real renderer, counting how often it actually renders."""

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir)
        self.calls = 0

    def render_all(self, svgs):
        result = super().render_all(svgs)
        self.calls += result.rendered
        return result


@pytest.fixture
def publisher(tmp_path: Path) -> Publisher:
    repo = tmp_path / "site"
    (repo / "content").mkdir(parents=True)
    config = QuartzConfig(
        repo_path=repo, remote="origin", branch="main", ssh_key_path=None,
        git_author="t <t@t>", build_locally=False,
    )
    return Publisher(config, tmp_path / "state", CountingRenderer(tmp_path / "state" / "png"))


def _note(vault: Vault, rel: str, text: str) -> tuple[str, float]:
    path = vault.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text, path.stat().st_mtime


def test_stage_attachments_copies_references_and_renders_png(vault, publisher) -> None:
    (vault.root / "Attachments" / "Places" / "Vauban.svg").write_bytes(SVG)
    (vault.root / "Attachments" / "Map.png").write_bytes(PNG)
    (vault.root / "Attachments" / "Inline.jpg").write_bytes(JPEG)
    published = {
        "Places/Vauban.md": _note(
            vault, "Places/Vauban.md",
            "---\npublish: true\nimage: Attachments/Places/Vauban.svg\n---\n\n# Vauban\n\n"
            "Intro.\n\n![[Map.png]]\n\n![x](../Attachments/Inline.jpg)\n![[Gone.png]]\n",
        )
    }
    plan = publisher.stage_attachments(vault, published)
    content = publisher.content_dir

    assert sorted(plan.added) == [
        "Attachments/Inline.jpg", "Attachments/Map.png",
        "Attachments/Places/Vauban.png", "Attachments/Places/Vauban.svg",
    ]
    assert plan.missing == [{"note": "Places/Vauban.md", "reference": "Gone.png"}]
    assert (content / "Attachments/Places/Vauban.svg").read_bytes() == SVG
    png = (content / "Attachments/Places/Vauban.png").read_bytes()
    assert png.startswith(b"\x89PNG")
    # 1200x630, from the IHDR chunk.
    assert int.from_bytes(png[16:20], "big") == 1200
    assert int.from_bytes(png[20:24], "big") == 630
    # Only the page image gets a PNG, not body embeds.
    assert not (content / "Attachments/Inline.png").exists()
    assert plan.outputs["Attachments/Places/Vauban.png"] == "public/attachments/places/vauban.png"

    # Second run: nothing changed, nothing rewritten, nothing re-rendered.
    renders = publisher._renderer.calls
    again = publisher.stage_attachments(vault, published)
    assert again.is_empty and again.png_cached == 1
    assert publisher._renderer.calls == renders

    # A changed image is an update, by content hash.
    (vault.root / "Attachments" / "Map.png").write_bytes(PNG + b"changed")
    third = publisher.stage_attachments(vault, published)
    assert third.updated == ["Attachments/Map.png"] and not third.added


def test_raster_page_image_is_used_as_is(vault, publisher) -> None:
    (vault.root / "Attachments" / "Hero.jpg").write_bytes(JPEG)
    published = {"N.md": _note(vault, "N.md", "---\nimage: Hero.jpg\n---\nBody\n")}
    plan = publisher.stage_attachments(vault, published)
    assert plan.added == ["Attachments/Hero.jpg"]
    assert plan.png_rendered == plan.png_cached == 0


def test_unreferenced_attachments_are_removed(vault, publisher) -> None:
    (vault.root / "Attachments" / "Places" / "Vauban.svg").write_bytes(SVG)
    published = {
        "Places/Vauban.md": _note(
            vault, "Places/Vauban.md",
            "---\nimage: Attachments/Places/Vauban.svg\n---\n# Vauban\n",
        )
    }
    publisher.stage_attachments(vault, published)
    content = publisher.content_dir
    # stage() drops an unpublished note's markdown before this runs.
    plan = publisher.stage_attachments(vault, {})
    assert sorted(plan.removed) == [
        "Attachments/Places/Vauban.png", "Attachments/Places/Vauban.svg"]
    assert not (content / "Attachments/Places/Vauban.svg").exists()


def test_sweep_keeps_images_a_published_page_still_names(vault, publisher) -> None:
    """Syncer-published pages can use an image in ways we do not parse."""
    content = publisher.content_dir
    (content / "Attachments").mkdir(parents=True)
    (content / "Attachments" / "Old Photo.jpg").write_bytes(JPEG)
    (content / "Attachments" / "Orphan.jpg").write_bytes(JPEG)
    (content / "Page.md").write_text('<img src="Attachments/Old%20Photo.jpg">\n')
    plan = publisher.stage_attachments(vault, {})
    assert plan.kept == ["Attachments/Old Photo.jpg"]
    assert plan.removed == ["Attachments/Orphan.jpg"]


def test_unsafe_vault_svg_is_not_published_or_removed(vault, publisher) -> None:
    evil = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    (vault.root / "Attachments" / "Evil.svg").write_bytes(evil)
    published = {"N.md": _note(vault, "N.md", "---\nimage: Attachments/Evil.svg\n---\nx\n")}
    plan = publisher.stage_attachments(vault, published)
    assert [r["path"] for r in plan.rejected] == ["Attachments/Evil.svg"]
    assert not plan.added
    assert not (publisher.content_dir / "Attachments/Evil.svg").exists()
    assert plan.png_rendered == 0


def test_skipped_dynamic_notes_keep_their_images(vault, publisher) -> None:
    (vault.root / "Attachments" / "Chart.png").write_bytes(PNG)
    published = {"D.md": _note(vault, "D.md", "![[Chart.png]]\n\n```dataview\nlist\n```\n")}
    plan = publisher.stage_attachments(vault, published)
    assert plan.added == ["Attachments/Chart.png"]


def test_attachment_plan_is_part_of_publish_plan_emptiness(vault, publisher) -> None:
    (vault.root / "Attachments" / "Map.png").write_bytes(PNG)
    published = {"N.md": _note(vault, "N.md", "---\npublish: true\n---\n![[Map.png]]\n")}
    plan = publisher.stage(vault, published)
    plan.attachments = publisher.stage_attachments(vault, published)
    assert not plan.is_empty
    out = plan.as_dict()
    assert out["attachments"]["added"] == ["Attachments/Map.png"]
    assert set(out["attachments"]) >= {"added", "updated", "removed", "missing"}


def test_bundled_fonts_are_shipped() -> None:
    from obsidian_mcp.render import BUNDLED_FONTS

    names = {p.name for p in BUNDLED_FONTS.glob("*.ttf")}
    assert {"SpaceGrotesk-Variable.ttf", "JetBrainsMono-Variable.ttf"} <= names


def test_validate_rejects_empty_and_unknown_types() -> None:
    with pytest.raises(att.InvalidAttachment):
        att.validate(b"", ".png")
    with pytest.raises(att.InvalidAttachment):
        att.validate(PNG, ".bmp")


def test_large_batches_render_in_worker_processes(tmp_path: Path) -> None:
    """The multi-worker path: enough misses to skip in-process rendering.

    A broken SVG fails alone; the rest of its batch still renders.
    """
    from obsidian_mcp import render

    renderer = PngRenderer(tmp_path / "cache")
    svgs = {
        f"img{i}.png": SVG.replace(b"Vauban", f"Place {i}".encode())
        for i in range(render.POOL_THRESHOLD + 2)
    }
    svgs["broken.png"] = b"<svg xmlns='http://www.w3.org/2000/svg'><unclosed></svg>"
    result = renderer.render_all(svgs)
    assert result.rendered == len(svgs) - 1
    assert list(result.failed) == ["broken.png"]
    for name, key in result.keys.items():
        assert renderer.read(key).startswith(b"\x89PNG"), name
    # Everything is cached now.
    again = renderer.render_all(svgs)
    assert again.rendered == 0 and again.cached == len(svgs) - 1


NFC_NAME = "Abbekås"   # what notes and git spell
NFD_NAME = "Abbekås"  # what a macOS-synced vault may hold on disk


def test_decomposed_vault_names_resolve_and_publish_composed(vault, publisher) -> None:
    (vault.root / "Attachments" / "Places" / f"{NFD_NAME}.svg").write_bytes(SVG)
    note = f"---\nimage: Attachments/Places/{NFC_NAME}.svg\n---\n# {NFC_NAME}\n"
    published = {f"Places/{NFD_NAME}.md": _note(vault, f"Places/{NFD_NAME}.md", note)}
    plan = publisher.stage_attachments(vault, published)
    assert plan.missing == []
    assert sorted(plan.added) == [
        f"Attachments/Places/{NFC_NAME}.png", f"Attachments/Places/{NFC_NAME}.svg"]
    names = {p.name for p in (publisher.content_dir / "Attachments" / "Places").iterdir()}
    assert names == {f"{NFC_NAME}.svg", f"{NFC_NAME}.png"}  # composed on the site


def test_note_sweep_keeps_notes_whose_vault_name_is_decomposed(vault, publisher) -> None:
    """Regression: every accented note was swept off the site on macOS."""
    site_note = publisher.content_dir / "Places" / f"{NFC_NAME}.md"
    site_note.parent.mkdir(parents=True)
    site_note.write_text("---\npublish: true\n---\n# A\n")
    published = {f"Places/{NFD_NAME}.md": _note(
        vault, f"Places/{NFD_NAME}.md", "---\npublish: true\n---\n# A\n")}
    plan = publisher.stage(vault, published)
    assert plan.removed == []
    assert [p.name for p in site_note.parent.iterdir()] == [f"{NFC_NAME}.md"]


def test_publish_result_caps_long_lists_but_keeps_counts() -> None:
    """A first image publish touched ~1,900 files; listing them all made an
    80-100 KB result that Claude clients could not take back inline."""
    from obsidian_mcp.quartz import RESULT_LIST_LIMIT, AttachmentPlan, PublishPlan

    many = [f"Attachments/Places/P{i:04}.svg" for i in range(1880)]
    plan = PublishPlan(added=["A.md"], updated=[], removed=[])
    plan.attachments = AttachmentPlan(added=many, removed=["Attachments/Old.png"])
    out = plan.as_dict()
    a = out["attachments"]
    assert len(a["added"]) == RESULT_LIST_LIMIT and a["added_count"] == 1880
    assert a["added"] == sorted(many)[:RESULT_LIST_LIMIT]
    assert a["removed"] == ["Attachments/Old.png"] and a["removed_count"] == 1
    assert out["truncated"] is True and "truncated_note" in out
    assert out["added"] == ["A.md"] and out["added_count"] == 1
    import json
    assert len(json.dumps(out)) < 10_000


def test_small_publish_result_is_unchanged_apart_from_counts() -> None:
    from obsidian_mcp.quartz import PublishPlan

    out = PublishPlan(added=["B.md", "A.md"], updated=[], removed=["C.md"]).as_dict()
    assert out["added"] == ["A.md", "B.md"] and out["removed"] == ["C.md"]
    assert out["truncated"] is False and "truncated_note" not in out


def test_missing_reference_is_listed_once_per_note(vault, publisher) -> None:
    text = "---\nimage: Attachments/Gone.svg\nsocialImage: Attachments/Gone.svg\n---\nx\n"
    plan = publisher.stage_attachments(vault, {"N.md": _note(vault, "N.md", text)})
    assert plan.missing == [{"note": "N.md", "reference": "Attachments/Gone.svg"}]


def test_missing_attachment_message_says_attachment(server) -> None:
    with pytest.raises(ToolFailed, match="No attachment at"):
        call(server, "vault_read_attachment", path="Attachments/Places/Nope.svg")


def test_long_skipped_list_sets_truncated() -> None:
    from obsidian_mcp.quartz import PublishPlan

    out = PublishPlan(added=[], updated=[], removed=[], skipped=[f"S{i}.md" for i in range(40)]).as_dict()
    assert out["skipped_count"] == 40 and len(out["skipped"]) == 25 and out["truncated"] is True
