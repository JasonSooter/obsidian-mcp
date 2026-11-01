"""SVG -> 1200x630 PNG for link previews, cached by content hash.

Facebook, LinkedIn and X do not accept SVG for og:image, so publish_site renders
each page image that is an SVG to a PNG beside it. resvg (via the resvg-py
wheel) does the rendering in-process: no system packages, no Node, no browser.

Rendering costs ~0.3 s an image and a place hero is ~150 KB of PNG, so results
are cached in the state directory under a key covering everything that affects
the output -- the SVG bytes, the resvg version, the fonts, the size. An
unchanged SVG therefore yields byte-identical PNG bytes on every publish, and
git sees no change.

resvg holds the GIL, so a large batch (the first publish after this feature
lands, ~300 images) is split across a few worker processes. They are plain
subprocesses running `python -m obsidian_mcp.render`, fed JSON on stdin -- not
multiprocessing, whose spawn/forkserver modes re-import the *caller's* main
script in every worker and so depend on how the server happened to be started.
"""

from __future__ import annotations

import hashlib
import re
import json
import logging
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1200, 630
# Bump to invalidate every cached PNG (e.g. after changing render options).
# Bump when the render pipeline changes output for the same SVG: it is part of the
# cache key, so cached PNGs from the old pipeline are re-rendered. 2: CSS variables
# are resolved before rendering (resvg ignores var(), so v2 heroes rendered black).
RENDER_VERSION = "2"
# Below this many misses, worker start-up costs more than it saves.
POOL_THRESHOLD = 8
MAX_WORKERS = 4
# Per worker batch; ~0.3 s an image, so this is generous for ~75 images.
WORKER_TIMEOUT_SECONDS = 600

BUNDLED_FONTS = Path(__file__).parent / "fonts"


@dataclass
class RenderResult:
    # key per input name, for inputs that rendered (or were cached)
    keys: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    rendered: int = 0
    cached: int = 0


_STYLE = re.compile(r"(<style[^>]*>)(.*?)(</style>)", re.S)
_VARS_RULE = re.compile(r"(#light:target~|#dark:target~)?#r\{((?:\s*--[\w-]+\s*:[^;}]*;?)+)\}")
_VAR_USE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)")


def resolve_css_vars(svg: str, theme: str = "dark") -> str:
    """Replace CSS custom properties in an SVG's <style> with their values.

    resvg supports class selectors but not var(): every fill and stroke that
    used one rendered as nothing, so v2 place heroes (themes as CSS variables)
    came out black apart from their one literal gradient. The hero convention is
    `#r{--k:v;...}` for the default (dark) theme and `#light:target~#r{...}` /
    `#dark:target~#r{...}` for the others; `theme` picks one. An SVG without
    variables is returned unchanged.
    """
    if "var(" not in svg:
        return svg

    def fix(m: re.Match[str]) -> str:
        css = m.group(2)
        found: dict[str, dict[str, str]] = {}
        for rule in _VARS_RULE.finditer(css):
            name = {"#light:target~": "light", "#dark:target~": "dark"}.get(rule.group(1) or "", "default")
            found.setdefault(name, {}).update(
                (k.strip(), v.strip()) for k, v in
                (d.split(":", 1) for d in rule.group(2).split(";") if ":" in d)
            )
        values = {**found.get("default", {}), **found.get(theme, {})}
        css = _VARS_RULE.sub("", css)
        css = _VAR_USE.sub(lambda u: values.get(u.group(1), u.group(2) or u.group(0)), css)
        return m.group(1) + css + m.group(3)

    return _STYLE.sub(fix, svg)


def _font_dirs() -> list[str]:
    dirs = [str(BUNDLED_FONTS)] if BUNDLED_FONTS.is_dir() else []
    extra = os.environ.get("OBSIDIAN_MCP_FONT_DIRS", "")
    dirs += [d for d in extra.split(os.pathsep) if d.strip()]
    return dirs


def _shrink(png: bytes) -> bytes:
    """Losslessly re-encode resvg's PNG smaller, or return it unchanged.

    resvg always writes RGBA, but hero images are fully opaque: dropping a
    constant alpha channel and re-deflating at maximum compression is ~19%
    smaller with identical pixels. Not part of the cache key on purpose, so
    previews already rendered (and published) are not re-encoded: that would
    add every one of them to the site's git history a second time.
    """
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(png))
        if image.mode == "RGBA":
            if image.getchannel("A").getextrema() != (255, 255):
                return png  # real transparency: keep it
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True, compress_level=9)
        smaller = out.getvalue()
        return smaller if len(smaller) < len(png) else png
    except Exception:  # noqa: BLE001 -- never fail a render over compression
        return png


def _render_to_file(svg: bytes, out: str, font_dirs: list[str]) -> None:
    """Worker: render one SVG and write the PNG atomically. Top-level so it pickles."""
    import resvg_py

    png = _shrink(bytes(
        resvg_py.svg_to_bytes(
            svg_string=resolve_css_vars(svg.decode("utf-8")),
            width=WIDTH,
            height=HEIGHT,
            font_dirs=font_dirs,
            # System fonts stay on: they are the fallback for glyphs the
            # bundled fonts lack (the Arabic in a few place names, say).
            skip_system_fonts=False,
        )
    ))
    target = Path(out)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".render.", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class PngRenderer:
    def __init__(self, cache_dir: Path) -> None:
        self._cache = cache_dir
        self._font_dirs = _font_dirs()
        self._salt = self._fingerprint()

    def _fingerprint(self) -> str:
        try:
            import resvg_py

            version = f"{resvg_py.__version__}/{resvg_py.__resvg_version__}"
        except ImportError:  # pragma: no cover -- a declared dependency
            version = "missing"
        fonts = []
        for directory in self._font_dirs:
            for font in sorted(Path(directory).rglob("*")):
                if font.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                    fonts.append(f"{font.name}:{font.stat().st_size}")
        return f"v{RENDER_VERSION}|{version}|{WIDTH}x{HEIGHT}|{','.join(fonts)}"

    def key(self, svg: bytes) -> str:
        digest = hashlib.sha256(self._salt.encode())
        digest.update(b"\0")
        digest.update(svg)
        return digest.hexdigest()

    def path_for(self, key: str) -> Path:
        return self._cache / f"{key}.png"

    def render_all(self, svgs: dict[str, bytes]) -> RenderResult:
        """Render every SVG (name -> bytes) that is not already cached."""
        self._cache.mkdir(parents=True, exist_ok=True)
        result = RenderResult()
        todo: dict[str, tuple[str, bytes]] = {}
        for name, svg in svgs.items():
            key = self.key(svg)
            if self.path_for(key).is_file():
                result.keys[name] = key
                result.cached += 1
            else:
                todo[name] = (key, svg)

        if not todo:
            return result

        if len(todo) < POOL_THRESHOLD:
            for name, (key, svg) in todo.items():
                self._run_one(result, name, key, svg)
            return result

        workers = max(1, min(MAX_WORKERS, os.cpu_count() or 1))
        items = list(todo.items())
        batches = [items[i::workers] for i in range(workers)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = pool.map(self._run_batch, [b for b in batches if b])
            for batch_result in outcomes:
                for name, key, error in batch_result:
                    if error is None:
                        result.keys[name] = key
                        result.rendered += 1
                    else:
                        result.failed[name] = error
        return result

    def _run_batch(self, batch: list[tuple[str, tuple[str, bytes]]]) -> list[tuple[str, str, str | None]]:
        """Render one batch in a worker subprocess. Never raises."""
        jobs = [
            {"svg": svg.decode("utf-8", errors="replace"), "out": str(self.path_for(key))}
            for _name, (key, svg) in batch
        ]
        payload = json.dumps({"font_dirs": self._font_dirs, "jobs": jobs})
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "obsidian_mcp.render"],
                input=payload, capture_output=True, text=True,
                timeout=WORKER_TIMEOUT_SECONDS, check=False,
            )
            if completed.returncode != 0:
                tail = (completed.stderr or "").strip().splitlines()
                raise RuntimeError(tail[-1] if tail else f"worker exited {completed.returncode}")
            errors = json.loads(completed.stdout)
            if not isinstance(errors, list) or len(errors) != len(batch):
                raise RuntimeError("worker returned a malformed result")
        except (subprocess.TimeoutExpired, OSError, ValueError, RuntimeError) as exc:
            failure = _describe(exc)
            return [(name, key, failure) for name, (key, _svg) in batch]
        return [
            (name, key, errors[i]) for i, (name, (key, _svg)) in enumerate(batch)
        ]

    def _run_one(self, result: RenderResult, name: str, key: str, svg: bytes) -> None:
        try:
            _render_to_file(svg, str(self.path_for(key)), self._font_dirs)
        except Exception as exc:  # noqa: BLE001 -- reported per file
            result.failed[name] = _describe(exc)
        else:
            result.keys[name] = key
            result.rendered += 1

    def read(self, key: str) -> bytes:
        return self.path_for(key).read_bytes()

    def prune(self, keep: set[str]) -> int:
        """Drop cached PNGs no published page uses any more."""
        if not self._cache.is_dir():
            return 0
        removed = 0
        for item in self._cache.glob("*.png"):
            if item.stem not in keep:
                item.unlink(missing_ok=True)
                removed += 1
        return removed


def _describe(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


def _worker() -> int:
    """`python -m obsidian_mcp.render`: render the jobs on stdin.

    Prints a JSON list with one entry per job: null on success, else the error.
    """
    request = json.load(sys.stdin)
    errors: list[str | None] = []
    for job in request["jobs"]:
        try:
            _render_to_file(job["svg"].encode("utf-8"), job["out"], request["font_dirs"])
        except Exception as exc:  # noqa: BLE001 -- reported per file
            errors.append(_describe(exc))
        else:
            errors.append(None)
    json.dump(errors, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(_worker())
