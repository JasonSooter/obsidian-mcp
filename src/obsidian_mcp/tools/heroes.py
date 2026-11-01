"""Place hero tools: render map heroes on the server, review them, commit the good ones.

Replaces a fragile path where a client generated each SVG elsewhere and then
retyped 16-50 KB of it into vault_write_attachment, which dropped bytes. Here a
client sends a few hundred bytes of job description per place; the server
renders, shows contact sheets, and writes only what the client approves.

Flow: start_hero_job -> hero_job_status (poll; results + contact sheets) ->
get_hero (any one at full size) -> commit_heroes (write the approved ones).
Rendering never touches the vault; commit_heroes is the only write.
"""

from __future__ import annotations

import posixpath
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from .. import attachments as att
from ..errors import ToolError
from ..hero.jobs import HERO_FOLDER, MAX_JOBS_PER_BATCH, HeroJobs, hero_path, note_path
from ..notes import parse, set_frontmatter_key
from ..telemetry import instrumented
from ._common import READ_ONLY, WRITES, Context


def register(mcp: MCPServer, ctx: Context, heroes: HeroJobs) -> None:
    @mcp.tool(annotations=WRITES)
    @instrumented("start_hero_job", exclusive=False)
    async def start_hero_job(jobs: list[dict[str, Any]], vault: str | None = None) -> dict[str, Any]:
        """Queue a batch of place heroes (1200x630 map SVGs, dark + light) for rendering.

        Nothing is written to the vault: rendering is a preview. Poll
        hero_job_status for results and contact sheets, then commit_heroes.

        `jobs`: up to 25 objects, rendered one after another (about 12-20 s
        each; all jobs on this server share one 1 request/s Nominatim limit):
        - required: `name` (the file name, no "/"), `lat`, `lng`
        - `icon`: book | newspaper | podcast | video; `confidence`: documented |
          inferred | best-guess | null
        - `osm` (e.g. "W62208778"), `query` (Nominatim search for the outline),
          `parent` (Nominatim search for the parent place) -- each or null
        - overrides: `subtitle`, `aliases` (list), `half` or `zoom` (view
          half-height in degrees), `pin_label: false` (no place label at the
          pin), `region: true` (frame as a region or country)
        - targets: `path` (default "Attachments/Places/<name>.svg"; must be an
          .svg under Attachments/Places/), `note` (default "Places/<name>.md";
          used by commit_heroes' set_image)

        Returns {job_id, status, count, jobs_ahead, estimated_seconds}.
        """
        target = ctx.vault(vault)
        return heroes.start(jobs, target.name)

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("hero_job_status")
    async def hero_job_status(job_id: str, sheets: bool = True) -> list[dict[str, Any] | Image]:
        """Progress and results of a hero job, plus contact sheets of what has rendered.

        status: queued | running | done | failed | interrupted (the server
        restarted; start the job again). Each result has index, name, path,
        bytes, over_target (above the ~32 KB size target), sha256, subtitle,
        pin_label, outline, lens, landmarks (wikipedia | nominatim | none), secs
        -- or error. Results fill in while the job runs.

        With sheets=true, also one JPEG per six rendered heroes: each hero dark
        and light side by side at 600 px wide, its file name above.
        """
        state = heroes.state(job_id)
        jobs = heroes.jobs(job_id)
        results = []
        for i, r in enumerate(state["results"]):
            if r is None:
                results.append({"index": i, "name": jobs[i]["name"], "status": "pending"})
                continue
            out = {**r, "path": hero_path(jobs[i])}
            if not r.get("error"):
                out["sha256"] = att.sha256(heroes.svg(job_id, i))
            results.append(out)
        done = sum(1 for r in state["results"] if r is not None)
        summary = {
            "job_id": job_id,
            "status": state["status"],
            "vault": state["vault"],
            "rendered": sum(1 for r in results if "bytes" in r),
            "failed": sum(1 for r in results if r.get("error")),
            "pending": len(results) - done,
            "results": results,
        }
        if state.get("error"):
            summary["error"] = state["error"]
        content: list[dict[str, Any] | Image] = [summary]
        if sheets and summary["rendered"]:
            content += [Image(data=jpg, format="jpeg") for jpg in heroes.sheets(job_id)]
        return content

    @mcp.tool(annotations=READ_ONLY)
    @instrumented("get_hero")
    async def get_hero(
        job_id: str | None = None,
        name: str | None = None,
        path: str | None = None,
        theme: str = "dark",
        vault: str | None = None,
    ) -> list[dict[str, Any] | Image]:
        """One hero as a full-size 1200x630 PNG, in the dark or light theme.

        Either a rendered job hero (`job_id` + `name`) or a hero SVG already in
        the vault (`path`, e.g. "Attachments/Places/Kandahar.svg").
        """
        if theme not in {"dark", "light"}:
            raise ToolError("theme must be 'dark' or 'light'.")
        if path:
            target = ctx.vault(vault)
            resolved = ctx.resolve_attachment(target, path, must_exist=True)
            if resolved.suffix.lower() != ".svg":
                raise ToolError("get_hero renders SVG heroes only.")
            svg = ctx.read_attachment(target, resolved)
            label = target.relative(resolved)
        elif job_id and name:
            jobs = heroes.jobs(job_id)
            index = next((i for i, j in enumerate(jobs) if j["name"] == name), None)
            if index is None:
                raise ToolError(f"No hero named {name!r} in job {job_id}.")
            svg = heroes.svg(job_id, index)
            label = hero_path(jobs[index])
        else:
            raise ToolError("Pass job_id and name, or path.")
        return [{"hero": label, "theme": theme}, Image(data=heroes.png(svg, theme), format="png")]

    @mcp.tool(annotations=WRITES)
    @instrumented("commit_heroes", mutation=True)
    async def commit_heroes(
        job_id: str, names: list[str], set_image: bool = False, vault: str | None = None
    ) -> dict[str, Any]:
        """Write approved heroes from a job into the vault.

        Each named hero goes to its job `path` (default
        "Attachments/Places/<name>.svg"). An existing file is saved to the
        version journal first (list_versions / restore_version undo it); an
        identical one is left alone.

        With set_image=true, also sets the note's `image` frontmatter (job
        `note`, default "Places/<name>.md") to the hero's path -- only when it
        is missing or already points under Attachments/Places/. A note whose
        `image` points anywhere else keeps its own image.

        Returns one entry per name: written | unchanged | skipped, with reasons.
        """
        if not names:
            raise ToolError("names must list at least one hero from the job.")
        if len(names) > MAX_JOBS_PER_BATCH:
            raise ToolError(f"At most {MAX_JOBS_PER_BATCH} names per call.")
        target = ctx.vault(vault)
        state = heroes.state(job_id)
        if state["vault"] != target.name:
            raise ToolError(f"Job {job_id} was rendered for vault {state['vault']!r}.")
        jobs = heroes.jobs(job_id)
        by_name = {j["name"]: i for i, j in enumerate(jobs)}
        out = []
        for name in names:
            index = by_name.get(name)
            result = state["results"][index] if index is not None else None
            if index is None:
                out.append({"name": name, "status": "skipped", "reason": "not in this job"})
                continue
            if not result or result.get("error"):
                out.append({"name": name, "status": "skipped", "reason": "not rendered"})
                continue
            out.append(_commit_one(target, jobs[index], heroes.svg(job_id, index), job_id, set_image))
        return {"job_id": job_id, "vault": target.name, "heroes": out}

    def _commit_one(target, job: dict[str, Any], data: bytes, job_id: str, set_image: bool) -> dict[str, Any]:
        rel = hero_path(job)
        if not rel.startswith(HERO_FOLDER):  # validated at start; checked again where it matters
            return {"name": job["name"], "status": "skipped", "reason": f"path is not under {HERO_FOLDER}"}
        resolved = ctx.resolve_attachment(target, rel, must_exist=False)
        if len(data) > ctx.config.max_attachment_bytes:
            return {"name": job["name"], "status": "skipped", "reason": "over the attachment size cap"}
        att.validate(data, resolved.suffix)
        digest = att.sha256(data)
        entry: dict[str, Any] = {"name": job["name"], "path": rel, "bytes": len(data), "sha256": digest}
        if resolved.is_symlink():
            return {**entry, "status": "skipped", "reason": "path is a symlink"}
        if resolved.is_file() and att.sha256(resolved.read_bytes()) == digest:
            entry["status"] = "unchanged"
        else:
            version_id = ctx.journal.snapshot(target, resolved, op="write")
            target.write_bytes_atomic(resolved, data)
            ctx.journal.record(tool="commit_heroes", vault=target.name, path=rel, version_id=version_id,
                               job_id=job_id, bytes=len(data), sha256=digest)
            entry.update(status="written", replaced=version_id is not None, previous_version_id=version_id)
        if set_image:
            entry["image"] = _set_image(target, note_path(job), rel)
        return entry

    def _set_image(target, note_rel: str, hero_rel: str) -> str:
        try:
            resolved = target.resolve(note_rel, must_exist=True)
        except Exception:  # noqa: BLE001 -- reported, never fatal to the commit
            return f"skipped: no note at {note_rel}"
        raw = ctx.read(target, resolved)
        current = parse(raw).metadata.get("image")
        if current == hero_rel:
            return "already set"
        if current and not _is_hero_path(str(current)):
            return f"skipped: the note uses its own image ({current})"
        version_id = ctx.journal.snapshot(target, resolved, op="patch")
        target.write_text_atomic(resolved, set_frontmatter_key(raw, "image", hero_rel))
        ctx.journal.record(tool="commit_heroes", vault=target.name, path=note_rel, version_id=version_id,
                           operation="set_image", image=hero_rel)
        return "set"


def _is_hero_path(value: str) -> bool:
    """Whether a note's `image` already points into the hero folder, after normalising.

    A plain prefix test let `Attachments/Places/../Photos/me.jpg` pass, so
    set_image would have replaced a note's own image.
    """
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        return False
    return posixpath.normpath(value).startswith(HERO_FOLDER)
