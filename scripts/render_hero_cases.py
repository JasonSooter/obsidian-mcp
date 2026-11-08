#!/usr/bin/env python3
"""Render the place-hero test cases to a folder, for eyeballing and diffing.

Previews only: nothing here touches a vault or the server. Each case is written
as <slug>.svg plus <slug>.dark.png and <slug>.light.png, rendered the way
contact sheets and get_hero render them (CSS variables resolved, then resvg),
and a summary.json with the generator's result line per case.

    HERO_DATA=/path/to/hero/data uv run python scripts/render_hero_cases.py OUTDIR \
        [--set cases|baseline|all] [--http-cache DIR] [--only SUBSTRING]

HERO_DATA must hold ne10.pkl, cities.json and world_{50,110}m.json (built by
`python -m obsidian_mcp.hero.generator --setup`, or copied from the server).
--http-cache stores Nominatim and Wikipedia responses on disk, so a second run
sees the same inputs as the first and its SVGs can be compared byte for byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ANTIMERIDIAN_CASES = [
    dict(slug="beringia-h1.5", name="Beringia", lat=65, lng=-170, half=1.5, pin_label=False),
    dict(slug="beringia-h2.2", name="Beringia", lat=65, lng=-170, half=2.2, pin_label=False),
    dict(slug="beringia-h3", name="Beringia", lat=65, lng=-170, half=3, pin_label=False),
    dict(slug="beringia-h6", name="Beringia", lat=65, lng=-170, half=6, pin_label=False),
    dict(slug="beringia-h12", name="Beringia", lat=65, lng=-170, half=12, pin_label=False),
    dict(slug="beringia-default", name="Beringia", lat=65, lng=-170, pin_label=False),
    # The default view of a region is polar at this latitude (half from Nominatim's bounding box).
    dict(slug="beringia-region", name="Beringia", lat=65, lng=-170, pin_label=False, region=True),
    dict(slug="bering-strait", name="Bering Strait", lat=65.75, lng=-168.75, query="Bering Strait"),
    dict(slug="kamchatka", name="Kamchatka", lat=56, lng=160, query="Kamchatka Peninsula"),
    dict(slug="chukotka", name="Chukotka", lat=66, lng=175, query="Chukotka Autonomous Okrug"),
    dict(slug="tau-default", name="Taʻū", lat=-14.23, lng=-169.47, query="Taʻū, American Samoa"),
    dict(slug="tau-h0.15", name="Taʻū", lat=-14.23, lng=-169.47, query="Taʻū, American Samoa", half=0.15),
    dict(slug="taveuni", name="Taveuni", lat=-16.85, lng=-179.97, query="Taveuni, Fiji"),
    dict(slug="longyearbyen", name="Longyearbyen", lat=78.22, lng=15.65, query="Longyearbyen"),
    dict(slug="mcmurdo", name="McMurdo Station", lat=-77.85, lng=166.67, query="McMurdo Station"),
    # The two above render at street scale by default; these force the polar (orthographic) view.
    dict(slug="longyearbyen-h4", name="Longyearbyen", lat=78.22, lng=15.65, query="Longyearbyen", half=4),
    dict(slug="mcmurdo-h4", name="McMurdo Station", lat=-77.85, lng=166.67, query="McMurdo Station", half=4),
    dict(slug="mcmurdo-h12", name="McMurdo Station", lat=-77.85, lng=166.67, query="McMurdo Station", half=12),
    dict(slug="north-pole", name="North Pole", lat=90, lng=0),
    dict(slug="freiburg", name="Freiburg im Breisgau", lat=47.995, lng=7.85, query="Freiburg im Breisgau"),
    dict(slug="zavodovski", name="Zavodovski Island", lat=-56.3, lng=-27.57, query="Zavodovski Island"),
]

BASELINE_CASES = [
    dict(slug="kandahar", name="Kandahar", lat=31.6289, lng=65.7372, query="Kandahar, Afghanistan"),
    dict(slug="freiburg", name="Freiburg im Breisgau", lat=47.995, lng=7.85, query="Freiburg im Breisgau"),
    dict(slug="bengal", name="Bengal", lat=23.7, lng=89.0, query="Bengal", region=True),
    dict(slug="yunnan", name="Yunnan", lat=25.0, lng=101.5, query="Yunnan"),
    dict(slug="tohoku", name="Tōhoku region", lat=39.0, lng=140.9, query="Tohoku region, Japan"),
    dict(slug="logan", name="Logan International Airport", lat=42.3656, lng=-71.0096, query="Logan International Airport"),
    dict(slug="theresienstadt", name="Theresienstadt", lat=50.5108, lng=14.1503, query="Terezín"),
    dict(slug="zavodovski", name="Zavodovski Island", lat=-56.3, lng=-27.57, query="Zavodovski Island"),
    dict(slug="tau-h0.15", name="Taʻū", lat=-14.23, lng=-169.47, query="Taʻū, American Samoa", half=0.15),
    dict(slug="beringia-h1.5", name="Beringia", lat=65, lng=-170, half=1.5, pin_label=False),
]


def _cases(which: str) -> list[dict]:
    sets = {"cases": ANTIMERIDIAN_CASES, "baseline": BASELINE_CASES,
            "all": ANTIMERIDIAN_CASES + BASELINE_CASES}[which]
    seen, out = set(), []
    for c in sets:
        if c["slug"] not in seen:
            seen.add(c["slug"]); out.append(c)
    return out


def _install_http_cache(g, cache_dir: Path) -> None:
    """Serve repeat requests from disk; skip the politeness sleep after a cache hit."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    real_fetch, real_sleep = g._fetch, time.sleep
    hit = [False]

    def fetch(url, timeout=120):
        fn = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".bin")
        if fn.exists():
            hit[0] = True
            return fn.read_bytes()
        hit[0] = False
        data = real_fetch(url, timeout)
        fn.write_bytes(data)
        return data

    class _Time:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def sleep(s):
            if not hit[0]:
                real_sleep(s)

    g._fetch = fetch
    g.time = _Time()


def _png(svg: str, theme: str) -> bytes:
    import resvg_py
    from obsidian_mcp.render import _font_dirs, resolve_css_vars

    return bytes(resvg_py.svg_to_bytes(svg_string=resolve_css_vars(svg, theme), width=1200, height=630,
                                       font_dirs=_font_dirs(), skip_system_fonts=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--set", default="all", choices=["cases", "baseline", "all"])
    ap.add_argument("--http-cache")
    ap.add_argument("--only", help="render only cases whose slug contains this")
    ap.add_argument("--no-png", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("HERO_DATA"):
        print("set HERO_DATA to the hero data folder", file=sys.stderr)
        return 2
    from obsidian_mcp.hero import generator as g

    if a.http_cache:
        _install_http_cache(g, Path(a.http_cache))
    g.load()
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for case in _cases(a.set):
        if a.only and a.only not in case["slug"]:
            continue
        job = {"icon": "book", "confidence": None, "osm": None, "query": None, "parent": None,
               **{k: v for k, v in case.items() if k != "slug"}}
        t0 = time.time()
        for tol, lite in g.RETRIES:
            g.TOL[0] = tol
            svg, r = g.make(job, str(out), lite)
            if len(svg.encode()) <= g.TARGET_BYTES:
                break
        r.update(bytes=len(svg.encode()), tol=tol, lite=lite, secs=round(time.time() - t0, 1))
        (out / f"{case['slug']}.svg").write_text(svg, encoding="utf-8")
        if not a.no_png:
            for theme in ("dark", "light"):
                (out / f"{case['slug']}.{theme}.png").write_bytes(_png(svg, theme))
        summary[case["slug"]] = r
        print(json.dumps({"slug": case["slug"], **{k: r[k] for k in ("bytes", "polar", "half", "tol", "lite")}}), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
