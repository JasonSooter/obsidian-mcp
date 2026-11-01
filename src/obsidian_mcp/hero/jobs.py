"""Hero render jobs: queue, run the generator, keep results for review.

A job is one batch of place heroes. start() validates it and queues it; a single
worker thread runs the generator (hero/generator.py) in a subprocess, one job at
a time, so every Nominatim request on this server goes through one process and
one 1-request-per-second pace. Results stream back one JSON line per hero and are
kept on disk under <state>/hero/jobs/<id>/, so a status call sees progress and
a restart does not lose finished renders (a job that was running is marked
interrupted).

Nothing here writes to the vault: rendering is a preview. The tools layer
commits the heroes a caller approves (tools/heroes.py).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import atexit
import queue
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ToolError

log = logging.getLogger(__name__)

MAX_JOBS_PER_BATCH = 25
SECONDS_PER_HERO = 180          # generous: real runs take 12-20 s; Nominatim retries can stretch one
SETUP_TIMEOUT = 3600            # first run downloads and builds ~1 GB of map data
KEEP_DAYS = 7
SHEET_HEROES = 6                # heroes per contact sheet
THUMB_W, THUMB_H = 600, 315     # each theme in a sheet: half size
LABEL_H = 30
ICONS = {"book", "newspaper", "podcast", "video"}
CONFIDENCE = {"documented", "inferred", "best-guess"}
OSM_ID = re.compile(r"^[NWR]\d+$")
HERO_FOLDER = "Attachments/Places/"

# key -> accepted types; anything else in a job is rejected so a typo cannot be silently ignored
_FIELDS: dict[str, tuple[type, ...]] = {
    "name": (str,), "lat": (int, float), "lng": (int, float), "icon": (str, type(None)),
    "confidence": (str, type(None)), "osm": (str, type(None)), "query": (str, type(None)),
    "parent": (str, type(None)), "subtitle": (str, type(None)), "aliases": (list, type(None)),
    "half": (int, float, type(None)), "zoom": (int, float, type(None)), "pin_label": (bool,),
    "region": (bool,), "note": (str, type(None)), "path": (str, type(None)),
}


def validate_jobs(jobs: Any) -> list[dict[str, Any]]:
    """Check a batch before it is queued; raise ToolError naming the first problem."""
    if not isinstance(jobs, list) or not jobs:
        raise ToolError("jobs must be a non-empty list of objects.")
    if len(jobs) > MAX_JOBS_PER_BATCH:
        raise ToolError(f"At most {MAX_JOBS_PER_BATCH} jobs per batch; got {len(jobs)}. Split it.")
    seen: set[str] = set()
    out = []
    for i, job in enumerate(jobs):
        where = f"jobs[{i}]"
        if not isinstance(job, dict):
            raise ToolError(f"{where} must be an object.")
        unknown = sorted(set(job) - set(_FIELDS))
        if unknown:
            raise ToolError(f"{where} has unknown field(s) {unknown}. Allowed: {sorted(_FIELDS)}.")
        for key in ("name", "lat", "lng"):
            if key not in job:
                raise ToolError(f"{where} is missing {key!r}.")
        for key, value in job.items():
            if isinstance(value, bool) and bool not in _FIELDS[key]:
                raise ToolError(f"{where}.{key} must not be a boolean.")
            if not isinstance(value, _FIELDS[key]):
                raise ToolError(f"{where}.{key} has the wrong type ({type(value).__name__}).")
        name = job["name"].strip()
        if not name or len(name) > 200 or "/" in name or "\\" in name or name.startswith("."):
            raise ToolError(f"{where}.name {job['name']!r} cannot be a file name.")
        if name in seen:
            raise ToolError(f"{where}.name {name!r} appears twice in this batch.")
        seen.add(name)
        if not (-90 <= job["lat"] <= 90 and -180 <= job["lng"] <= 180):
            raise ToolError(f"{where}: lat/lng out of range.")
        if job.get("icon") not in ICONS | {None}:
            raise ToolError(f"{where}.icon must be one of {sorted(ICONS)}.")
        if job.get("confidence") not in CONFIDENCE | {None}:
            raise ToolError(f"{where}.confidence must be one of {sorted(CONFIDENCE)} or null.")
        if job.get("osm") is not None and not OSM_ID.match(job["osm"]):
            raise ToolError(f"{where}.osm must look like 'W62208778' (N, W or R plus digits).")
        if job.get("aliases") is not None and not all(isinstance(a, str) for a in job["aliases"]):
            raise ToolError(f"{where}.aliases must be a list of strings.")
        if job.get("pin_label") is True:
            raise ToolError(f"{where}.pin_label only accepts false (omit it for the default).")
        path = job.get("path")
        if path is not None and not (path.startswith(HERO_FOLDER) and path.endswith(".svg") and ".." not in path):
            raise ToolError(f"{where}.path must be an .svg under {HERO_FOLDER}")
        note = job.get("note")
        if note is not None and (not note.endswith(".md") or ".." in note):
            raise ToolError(f"{where}.note must be a vault-relative .md path.")
        out.append({**job, "name": name})
    return out


def hero_path(job: dict[str, Any]) -> str:
    return job.get("path") or f"{HERO_FOLDER}{job['name']}.svg"


def note_path(job: dict[str, Any]) -> str:
    return job.get("note") or f"Places/{job['name']}.md"


class HeroJobs:
    """The job queue. One instance per server; one worker thread."""

    def __init__(
        self, root: Path, user_agent: str, font_dirs: list[str], *, command: list[str] | None = None
    ) -> None:
        """`command` replaces the generator (tests); it then also skips the map-data setup."""
        self.root = root
        self.data_dir = root / "data"
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._env = {**os.environ, "HERO_DATA": str(self.data_dir), "HERO_USER_AGENT": user_agent}
        self._font_dirs = font_dirs
        self._command = command or [sys.executable, "-m", "obsidian_mcp.hero.generator"]
        self._needs_data = command is None
        self._lock = threading.Lock()
        self._sheet_lock = threading.Lock()
        # A daemon thread, not a ThreadPoolExecutor: the executor's worker is joined at
        # interpreter exit, so a 25-place render held up a restart. The exit hook kills a
        # running generator so it does not outlive the server either.
        self._queue: queue.Queue[str] = queue.Queue()
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()   # spawn and stop() never interleave
        self._stopping = False
        threading.Thread(target=self._work, name="hero", daemon=True).start()
        atexit.register(self.stop)
        self._recover()

    # -- state on disk -------------------------------------------------------

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{6}", job_id or ""):
            raise ToolError(f"No hero job {job_id!r}.")
        d = self.jobs_dir / job_id
        if not d.is_dir():
            raise ToolError(f"No hero job {job_id!r} (jobs are kept {KEEP_DAYS} days).")
        return d

    def _read_state(self, d: Path) -> dict[str, Any]:
        return json.loads((d / "state.json").read_text(encoding="utf-8"))

    def _write_state(self, d: Path, state: dict[str, Any]) -> None:
        tmp = d / "state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "state.json")

    def _update(self, d: Path, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self._read_state(d)
            state.update(changes)
            self._write_state(d, state)
            return state

    def _recover(self) -> None:
        """At startup: a job that was queued or running when the server stopped will never finish."""
        for d in self.jobs_dir.iterdir():
            try:
                state = self._read_state(d)
            except (OSError, ValueError):
                continue
            if state.get("status") in {"queued", "running"}:
                state["status"] = "interrupted"
                state["error"] = "The server restarted before this job finished. Start it again."
                self._write_state(d, state)

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for d in self.jobs_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)

    # -- API -------------------------------------------------------------------

    def start(self, jobs: list[dict[str, Any]], vault: str) -> dict[str, Any]:
        jobs = validate_jobs(jobs)
        self._prune()
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        d = self.jobs_dir / job_id
        d.mkdir()
        (d / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        ahead = sum(1 for s in self._states() if s.get("status") in {"queued", "running"})
        self._write_state(d, {"job_id": job_id, "vault": vault, "status": "queued", "count": len(jobs),
                              "created": time.time(), "results": [None] * len(jobs)})
        self._queue.put(job_id)
        return {"job_id": job_id, "status": "queued", "count": len(jobs), "jobs_ahead": ahead,
                "estimated_seconds": (ahead * MAX_JOBS_PER_BATCH + len(jobs)) * 20}

    def _states(self) -> list[dict[str, Any]]:
        out = []
        for d in self.jobs_dir.iterdir():
            try:
                out.append(self._read_state(d))
            except (OSError, ValueError):
                continue
        return out

    def state(self, job_id: str) -> dict[str, Any]:
        return self._read_state(self._dir(job_id))

    def jobs(self, job_id: str) -> list[dict[str, Any]]:
        return json.loads((self._dir(job_id) / "jobs.json").read_text(encoding="utf-8"))

    def svg(self, job_id: str, index: int) -> bytes:
        f = self._dir(job_id) / "out" / f"{index}.svg"
        if not f.is_file():
            raise ToolError(f"Hero {index} of job {job_id} has not been rendered.")
        return f.read_bytes()

    # -- worker ----------------------------------------------------------------

    def _work(self) -> None:
        while True:
            self._run(self._queue.get())

    def stop(self) -> None:
        """Kill a running generator (at exit). The job is marked interrupted at the next start."""
        with self._proc_lock:
            self._stopping = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _generator(self, *args: str, timeout: float, stdout_handler=None) -> int:
        cmd = [*self._command, *args]
        with self._proc_lock:
            if self._stopping:   # shutting down: never start a child the exit hook could miss
                return -1
            proc = subprocess.Popen(cmd, env=self._env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8")
            self._proc = proc
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if stdout_handler and line.strip():
                    stdout_handler(line)
            stderr = proc.stderr.read() if proc.stderr else ""
            code = proc.wait()
        finally:
            timer.cancel()
            self._proc = None
        if code != 0 and stderr:
            log.warning("hero generator %s exited %s: %s", args[:1], code, stderr[-2000:])
        return code

    def _ensure_data(self) -> None:
        if not self._needs_data:
            return
        if (self.data_dir / "ne10.pkl").is_file() and (self.data_dir / "cities.json").is_file():
            return
        code = self._generator("--setup", timeout=SETUP_TIMEOUT)
        if code != 0:
            raise RuntimeError("Map data setup failed; see the server log.")

    def _run(self, job_id: str) -> None:
        d = self.jobs_dir / job_id
        try:
            self._update(d, status="running", started=time.time())
            self._ensure_data()
            jobs = json.loads((d / "jobs.json").read_text(encoding="utf-8"))

            def take(line: str) -> None:
                try:
                    r = json.loads(line)
                except ValueError:
                    return
                if not isinstance(r, dict) or not isinstance(r.get("index"), int):
                    return
                with self._lock:
                    state = self._read_state(d)
                    state["results"][r["index"]] = r
                    self._write_state(d, state)

            code = self._generator(str(d / "jobs.json"), str(d / "out"),
                                   timeout=SECONDS_PER_HERO * len(jobs), stdout_handler=take)
            state = self.state(job_id)
            missing = [i for i, r in enumerate(state["results"]) if r is None]
            for i in missing:
                state["results"][i] = {"index": i, "name": jobs[i]["name"],
                                       "error": "The generator stopped before this hero (timeout or crash)."}
            self._update(d, results=state["results"], status="done" if code == 0 else "failed",
                         finished=time.time(), exit_code=code)
        except Exception as exc:  # noqa: BLE001 -- recorded on the job; the worker must survive
            log.exception("hero job %s failed", job_id)
            self._update(d, status="failed", error=f"{type(exc).__name__}: {exc}", finished=time.time())

    # -- images ----------------------------------------------------------------

    def png(self, svg: bytes, theme: str, width: int = 1200, height: int = 630) -> bytes:
        """One theme of a two-theme hero SVG, rendered to PNG."""
        import resvg_py

        from ..render import resolve_css_vars

        # resvg supports neither :target nor var(), so pick the theme's values up front.
        text = resolve_css_vars(svg.decode("utf-8"), "light" if theme == "light" else "dark")
        return bytes(resvg_py.svg_to_bytes(svg_string=text, width=width, height=height,
                                           font_dirs=self._font_dirs, skip_system_fonts=False))

    def sheets(self, job_id: str) -> list[bytes]:
        """Contact sheets (JPEG) of every rendered hero: up to six per sheet, dark and light side by side,
        each at 600 px wide with the file name above. Cached per job until more heroes finish."""
        from PIL import Image, ImageDraw, ImageFont

        d = self._dir(job_id)
        state = self._read_state(d)
        jobs = self.jobs(job_id)
        done = [r for r in state["results"] if r and not r.get("error")]
        key = f"{len(done)}"
        cache = d / "sheets" / key
        with self._sheet_lock:
            if cache.is_dir():
                return [p.read_bytes() for p in sorted(cache.glob("*.jpg"))]
            font = ImageFont.load_default(size=18)
            out = []
            for start in range(0, len(done), SHEET_HEROES):
                group = done[start:start + SHEET_HEROES]
                sheet = Image.new("RGB", (THUMB_W * 2, (THUMB_H + LABEL_H) * len(group)), "#20242b")
                draw = ImageDraw.Draw(sheet)
                for row, r in enumerate(group):
                    y = row * (THUMB_H + LABEL_H)
                    label = hero_path(jobs[r["index"]]).rsplit("/", 1)[-1]
                    draw.text((10, y + 5), f"{label}   ({r.get('bytes', 0) // 1024} KB)", fill="#eceff4", font=font)
                    svg = self.svg(job_id, r["index"])
                    for col, theme in enumerate(("dark", "light")):
                        thumb = Image.open(io.BytesIO(self.png(svg, theme, THUMB_W, THUMB_H))).convert("RGB")
                        sheet.paste(thumb, (col * THUMB_W, y + LABEL_H))
                buf = io.BytesIO()
                sheet.save(buf, "JPEG", quality=82, optimize=True)
                out.append(buf.getvalue())
            cache.mkdir(parents=True, exist_ok=True)
            for i, data in enumerate(out):
                (cache / f"{i:02}.jpg").write_bytes(data)
            return out
