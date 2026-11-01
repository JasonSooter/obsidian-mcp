"""Place hero tools, end to end through a real MCPServer, with a stand-in generator.

The real generator needs ~1 GB of map data and network access (Nominatim,
Wikipedia), so the job runner is pointed at a tiny script that writes valid
two-theme SVGs and reports results in the generator's JSON-lines format.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as SDKToolError

from obsidian_mcp.config import Config
from obsidian_mcp.errors import ToolError
from obsidian_mcp.hero.jobs import HeroJobs, validate_jobs
from obsidian_mcp.journal import Journal
from obsidian_mcp.tools import heroes as hero_tools
from obsidian_mcp.tools._common import Context
from obsidian_mcp.vault import Vault, VaultRegistry

FAKE_GENERATOR = r'''
import json, sys
jobs = json.load(open(sys.argv[1])); out = sys.argv[2]
import os; os.makedirs(out, exist_ok=True)
for i, j in enumerate(jobs):
    if j["name"] == "Broken":
        print(json.dumps({"index": i, "name": j["name"], "error": "ValueError: no map"}), flush=True); continue
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">'
           '<style>#r{--bg:#2e3440;--acc:#b48ead}#light:target~#r{--bg:#eceff4;--acc:#9a6d93}'
           '#dark:target~#r{--bg:#2e3440;--acc:#b48ead}.pb{fill:var(--bg)}.ac{fill:var(--acc)}</style>'
           '<g id="light"/><g id="dark"/><g id="r"><rect class="pb" width="1200" height="630"/>'
           '<rect class="ac" x="500" y="250" width="200" height="130"/></g></svg>')
    open(os.path.join(out, f"{i}.svg"), "w").write(svg)
    print(json.dumps({"index": i, "name": j["name"], "bytes": len(svg), "subtitle": "Somewhere",
                      "landmarks": "none", "over_target": False}), flush=True)
'''


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "MyVault"
    (root / "Attachments" / "Places").mkdir(parents=True)
    (root / "Places").mkdir()
    return Vault(name="MyVault", root=root.resolve())


@pytest.fixture
def setup(tmp_path: Path, vault: Vault):
    config = Config(
        vault_root=vault.root.parent, vault_names=(vault.name,), default_vault=vault.name,
        state_dir=tmp_path / "state", login_password="x" * 12, totp_secret="JBSWY3DPEHPK3PXP",
        host="127.0.0.1", port=0, public_url="http://localhost", max_file_bytes=2_000_000,
        journal_retention_days=30, environment="test", quartz=None,
        heroes_enabled=True, hero_user_agent="test",
    )
    ctx = Context(config=config, vaults=VaultRegistry({vault.name: vault}, vault.name),
                  journal=Journal(config.versions_dir, config.audit_log_path, retention_days=30),
                  publisher=None)
    script = tmp_path / "fake_generator.py"
    script.write_text(FAKE_GENERATOR)
    jobs = HeroJobs(config.hero_dir, "test", [], command=[sys.executable, str(script)])
    mcp = MCPServer(name="test")
    hero_tools.register(mcp, ctx, jobs)
    return mcp, jobs, ctx


class ToolFailed(Exception):
    pass


def call(mcp: MCPServer, tool: str, **args):
    try:
        result = asyncio.run(mcp.call_tool(tool, args))
    except SDKToolError as exc:
        raise ToolFailed(f"{exc} {exc.__cause__ or ''}") from exc
    if result.is_error:
        raise ToolFailed(result.content[0].text)
    return json.loads(result.content[0].text), result.content[1:]


def _wait(jobs: HeroJobs, job_id: str) -> dict:
    for _ in range(200):
        state = jobs.state(job_id)
        if state["status"] in {"done", "failed", "interrupted"}:
            return state
        time.sleep(0.05)
    raise AssertionError("job did not finish")


JOB = {"name": "Vauban", "lat": 47.98, "lng": 7.83, "icon": "book"}


def test_validate_jobs_rejects_what_would_go_wrong_later() -> None:
    assert validate_jobs([JOB])[0]["name"] == "Vauban"
    bad = [
        ([], "non-empty"),
        ([{**JOB, "colour": "red"}], "unknown field"),
        ([{"name": "X", "lat": 1}], "missing 'lng'"),
        ([{**JOB, "name": "a/b"}], "file name"),
        ([JOB, JOB], "twice"),
        ([{**JOB, "lat": 91}], "out of range"),
        ([{**JOB, "icon": "map"}], "icon"),
        ([{**JOB, "osm": "62208778"}], "osm"),
        ([{**JOB, "pin_label": True}], "only accepts false"),
        ([{**JOB, "path": "Attachments/Other/V.svg"}], "Attachments/Places/"),
        ([{**JOB, "path": "Attachments/Places/../V.svg"}], "Attachments/Places/"),
        ([{**JOB, "half": True}], "boolean"),
        ([{**JOB, "name": f"P{i}"} for i in range(26)], "At most 25"),
    ]
    for jobs, message in bad:
        with pytest.raises(ToolError, match=message):
            validate_jobs(jobs)


def test_render_review_and_commit(setup, vault) -> None:
    mcp, jobs, _ = setup
    (vault.root / "Places" / "Vauban.md").write_text("---\ntitle: Vauban\n---\n\nBody.\n")
    (vault.root / "Places" / "Mine.md").write_text("---\nimage: Attachments/Photos/me.jpg\n---\n\nBody.\n")
    started, _ = call(mcp, "start_hero_job", jobs=[JOB, {**JOB, "name": "Mine"}, {**JOB, "name": "Broken"}])
    job_id = started["job_id"]
    assert started["count"] == 3
    assert not (vault.root / "Attachments" / "Places" / "Vauban.svg").exists()  # preview only
    assert _wait(jobs, job_id)["status"] == "done"

    status, images = call(mcp, "hero_job_status", job_id=job_id)
    assert (status["rendered"], status["failed"], status["pending"]) == (2, 1, 0)
    first = status["results"][0]
    assert first["path"] == "Attachments/Places/Vauban.svg" and len(first["sha256"]) == 64
    assert status["results"][2]["error"].startswith("ValueError")
    assert len(images) == 1 and images[0].mime_type == "image/jpeg"

    # The contact sheet shows real colours, not the black resvg draws for var().
    from PIL import Image as PILImage
    import base64
    sheet = PILImage.open(io.BytesIO(base64.b64decode(images[0].data))).convert("RGB")
    def near(px, rgb):  # JPEG shifts colours by a few levels
        return all(abs(a - b) <= 6 for a, b in zip(px, rgb))
    assert near(sheet.getpixel((300, 30 + 157)), (0xB4, 0x8E, 0xAD))     # dark accent
    assert near(sheet.getpixel((900, 30 + 157)), (0x9A, 0x6D, 0x93))     # light accent

    info, png = call(mcp, "get_hero", job_id=job_id, name="Vauban", theme="light")
    assert info["theme"] == "light" and png[0].mime_type == "image/png"

    out, _ = call(mcp, "commit_heroes", job_id=job_id, names=["Vauban", "Mine", "Broken", "Nope"],
                  set_image=True)
    by = {h["name"]: h for h in out["heroes"]}
    assert by["Vauban"]["status"] == "written" and by["Vauban"]["image"] == "set"
    assert "image: Attachments/Places/Vauban.svg" in (vault.root / "Places" / "Vauban.md").read_text()
    assert by["Mine"]["image"].startswith("skipped: the note uses its own image")
    assert "Attachments/Photos/me.jpg" in (vault.root / "Places" / "Mine.md").read_text()
    assert by["Broken"]["reason"] == "not rendered" and by["Nope"]["reason"] == "not in this job"

    # Committing the same hero again changes nothing; a changed file is journalled first.
    again, _ = call(mcp, "commit_heroes", job_id=job_id, names=["Vauban"], set_image=True)
    assert again["heroes"][0]["status"] == "unchanged" and again["heroes"][0]["image"] == "already set"
    (vault.root / "Attachments" / "Places" / "Vauban.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    third, _ = call(mcp, "commit_heroes", job_id=job_id, names=["Vauban"])
    assert third["heroes"][0]["replaced"] is True and third["heroes"][0]["previous_version_id"]


def test_unknown_job_and_restart_recovery(setup, tmp_path) -> None:
    mcp, jobs, _ = setup
    with pytest.raises(ToolFailed, match="No hero job"):
        call(mcp, "hero_job_status", job_id="../../etc")
    started, _ = call(mcp, "start_hero_job", jobs=[JOB])
    _wait(jobs, started["job_id"])
    # A job left "running" by a crash is reported as interrupted after a restart.
    d = jobs.jobs_dir / started["job_id"]
    state = json.loads((d / "state.json").read_text())
    state["status"] = "running"
    (d / "state.json").write_text(json.dumps(state))
    restarted = HeroJobs(jobs.root, "test", [], command=["true"])
    assert restarted.state(started["job_id"])["status"] == "interrupted"


def test_generator_helpers_without_map_data() -> None:
    from obsidian_mcp.hero import generator as g

    assert g.contested(31.7183, 35.1953) and not g.contested(47.98, 7.83)
    assert g.region_half({"boundingbox": ["36", "40", "-84", "-78"]}, 37.5, -81.5) == pytest.approx(2.4)
    assert g.region_half({"boundingbox": ["0", "1", "0", "1"]}, 50, 50) == 6.0   # box not around the pin
    assert g.region_half(None, 0, 0) == 6.0
