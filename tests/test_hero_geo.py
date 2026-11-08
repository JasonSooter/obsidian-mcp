"""Place heroes across the 180° line and near the poles (hero/geo.py, generator.qv/land/polar_map).

No map data needed: the generator tests swap in tiny synthetic layers that, like Natural
Earth, are split at ±180°.
"""
from __future__ import annotations

import math
import re

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.strtree import STRtree

from obsidian_mcp.hero import geo


def test_world_copies_of_a_view_inside_the_world_is_the_view() -> None:
    v = box(5, 40, 10, 50)
    assert [(p, s) for p, s in geo.world_copies(v)] == [(v, 0.0)]
    assert not geo.crosses(v)


@pytest.mark.parametrize("view, shifts", [
    (box(-190, 60, -170, 70), {0.0, -360.0}),   # west of -180: the far side is at +170..+180
    (box(170, -20, 190, -10), {0.0, 360.0}),     # east of +180
    (box(-560, 0, -500, 10), {-720.0, -360.0}),  # a turn and more out still maps back
])
def test_world_copies_map_every_part_back_into_the_data_range(view, shifts) -> None:
    got = list(geo.world_copies(view))
    assert {s for _, s in got} == shifts
    for part, s in got:
        x0, _, x1, _ = part.bounds
        assert -180 <= x0 and x1 <= 180
    assert sum(p.area for p, _ in got) == pytest.approx(view.area)


def test_wrap_and_dlng() -> None:
    assert geo.wrap_lng(179.9, -179.97) == pytest.approx(-180.1)
    assert geo.wrap_lng(-179.9, 179.0) == pytest.approx(180.1)
    assert geo.wrap_lng(7.85, 7.0) == pytest.approx(7.85)
    assert geo.dlng(179.9, -179.9) == pytest.approx(-0.2)
    assert geo.dlng(-179.9, 179.9) == pytest.approx(0.2)
    assert geo.dlng(10, 5) == 5


def _split_square(x_span=0.2, y0=-17, y1=-16.6):
    """A small island straddling 180°, the way Natural Earth and Nominatim store it: two parts."""
    return MultiPolygon([box(180 - x_span, y0, 180, y1), box(-180, y0, -180 + x_span, y1)])


def test_unwrap_joins_an_outline_split_at_the_line() -> None:
    g = _split_square()
    u = geo.unwrap(g, -179.97)
    assert u.geom_type == "Polygon"             # one piece: no seam stroke down the middle
    assert u.bounds == pytest.approx((-180.2, -17, -179.8, -16.6))
    assert u.area == pytest.approx(g.area)
    assert geo.max_lng_jump(u) < 1
    # ...and on the other side of the line it lands east of +180 instead
    assert geo.unwrap(g, 179.97).bounds == pytest.approx((179.8, -17, 180.2, -16.6))


def test_unwrap_leaves_geometry_away_from_the_line_untouched() -> None:
    g = MultiPolygon([box(7, 47, 8, 48), box(9, 47, 10, 48)])
    assert geo.unwrap(g, 7.85) is g             # the same object: byte-identical output downstream
    assert geo.unwrap(None, 0) is None


def test_cap_crossing_the_line() -> None:
    c = geo.cap(65, -170, 10)
    assert c.is_valid
    x0, y0, x1, y1 = c.bounds
    assert x0 < -180 < x1                       # continuous longitude, not split
    assert c.contains(Point(-178, 65)) and c.contains(Point(-182, 65))   # 178°E is -182 here
    assert not c.contains(Point(-170, 77))
    assert y0 == pytest.approx(55) and y1 == pytest.approx(75)


@pytest.mark.parametrize("lat", [80, -77.85, 89.5])
def test_cap_holding_a_pole_is_a_band_to_that_pole(lat) -> None:
    lng = 166.67
    c = geo.cap(lat, lng, 20)
    assert c.is_valid
    pole = math.copysign(89.9, lat)
    for dx in (-179, -90, 0, 90, 179):          # every meridian reaches the pole
        assert c.contains(Point(lng + dx, pole))
    assert c.bounds[0] == pytest.approx(lng - 180) and c.bounds[2] == pytest.approx(lng + 180)
    # a point 25° away on the far side of the pole is outside; 15° away is inside
    far = math.copysign(180 - abs(lat) - 25 + 0.0, lat) if abs(lat) < 89 else None
    if far is not None and abs(far) < 90:
        assert not c.contains(Point(lng + 180 - 1e-6, far))


def test_cap_rejects_a_hemisphere_or_more() -> None:
    with pytest.raises(ValueError):
        geo.cap(0, 0, 90)


def test_ortho_xy_matches_the_scalar_projection() -> None:
    from obsidian_mcp.hero.generator import ortho
    import numpy as np

    f, g = geo.ortho_xy(65, -170, 600, 244, 2941), ortho(65, -170, 600, 244, 2941)
    pts = [(-170, 65), (-182, 63), (178, 63), (-160, 70)]
    got = f(np.array(pts, dtype=float))
    for (lo, la), (x, y) in zip(pts, got):
        gx, gy, vis = g(lo, la)
        assert vis and (x, y) == pytest.approx((gx, gy))
    # 178°E and -182° are the same place
    assert got[1] == pytest.approx(got[2])


def test_oriented_polygons_keep_holes_open_under_nonzero() -> None:
    p = Polygon([(0, 0), (0, 10), (10, 10), (10, 0)], [[(2, 2), (8, 2), (8, 8), (2, 8)]])  # both clockwise
    o = geo.oriented(p)
    assert o.exterior.is_ccw and not o.interiors[0].is_ccw


def test_projected_seam_leaves_no_wide_jumps() -> None:
    """Both halves of land split at 180°, projected for a view centred on the line: one ring, short steps."""
    import numpy as np
    import shapely

    halves = [box(170, 60, 180, 70), box(-180, 60, -170, 70)]
    view = geo.cap(65, -178, 15)
    pieces = []
    for part, s in geo.world_copies(view):
        for h in halves:
            x = h.intersection(part)
            if not x.is_empty:
                pieces.append(shapely.affinity.translate(x, s, 0) if s else x)
    merged = shapely.unary_union(pieces)
    assert merged.geom_type == "Polygon" and not list(merged.interiors)
    f = geo.ortho_xy(65, -178, 600, 244, 1000)
    px = geo.project(shapely.segmentize(merged, 0.5), f, box(-1e4, -1e4, 1e4, 1e4))
    xy = np.asarray(px.exterior.coords)
    assert np.hypot(*np.diff(xy, axis=0).T).max() < 50   # a wrong-way ring would jump ~the globe's width


# --- the generator, on synthetic map data -------------------------------------------------------

@pytest.fixture
def synthetic(monkeypatch):
    """Land: Chukotka-like block split at 180°, plus Alaska; a river split at 180°; a border."""
    from obsidian_mcp.hero import generator as g

    layers = {
        "land": [(box(170, 60, 180, 70), {}), (box(-180, 60, -172, 70), {}), (box(-168, 60, -150, 70), {})],
        "lakes": [(box(175, 64, 176, 65), {"n": "Lake", "r": 3})],
        "rivers": [(LineString([(178, 66), (180, 66.5)]), {"n": "River", "r": 2}),
                   (LineString([(-180, 66.5), (-175, 67)]), {"n": "River", "r": 2})],
        "adm0": [(LineString([(-141, 60), (-141, 70)]), {})],
        "countries": [(box(170, 60, 180, 70), {"n": "Russia", "x": 175.0, "y": 65.0, "r": 2})],
    }
    for k in ("regions", "urban", "adm1", "rail", "roads", "regpts", "peaks", "marine", "airports", "ports", "coast"):
        layers.setdefault(k, [])
    monkeypatch.setattr(g, "NE10", layers)
    monkeypatch.setattr(g, "TREES", {k: STRtree([x for x, _ in v]) for k, v in layers.items()})
    monkeypatch.setattr(g, "TOL", [1.2])
    return g


def test_qv_brings_the_far_side_of_the_line_into_a_crossing_view(synthetic) -> None:
    g = synthetic
    view = box(-185, 62, -165, 68)
    got = g.qv("land", view)
    xs = sorted(round(x.bounds[0], 6) for x, _ in got)
    assert xs[0] == pytest.approx(-185)          # the +170..180 block, shifted and clipped
    assert all(geo.max_lng_jump(x) < 180 for x, _ in got)
    rus = g.qv("countries", view)
    assert rus and rus[0][1]["x"] == pytest.approx(175 - 360)   # label point shifted with it
    assert g.qv("land", box(-160, 62, -155, 68)) == g.q("land", box(-160, 62, -155, 68))


def test_land_merges_the_split_so_no_seam_is_stroked(synthetic) -> None:
    g = synthetic
    view = box(-185, 62, -165, 68)
    polys = g.land(view)
    assert len(polys) == 1
    parts = list(polys[0].geoms) if hasattr(polys[0], "geoms") else [polys[0]]
    # Chukotka block (-185..-172) is one polygon; Alaska (-168..-165) the other
    assert sorted(round(p.bounds[0]) for p in parts) == [-185, -168]


def _raster(g, shapes, theme="light"):
    import io

    import resvg_py
    from PIL import Image

    from obsidian_mcp.render import resolve_css_vars

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">{g.css(g.DARK, g.LIGHT, False)}'
           f'<g id="light"/><g id="dark"/><g id="r"><rect class="ps" width="1200" height="630"/>{"".join(shapes)}</g></svg>')
    png = bytes(resvg_py.svg_to_bytes(svg_string=resolve_css_vars(svg, theme), width=1200, height=630))
    return Image.open(io.BytesIO(png)).convert("RGB")


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_polar_map_fills_land_as_land_and_sea_as_sea(synthetic, theme) -> None:
    """The Beringia bug: polar views painted the sea black and land as cut-outs. Rasterise and look."""
    g = synthetic
    lat, lng, half = 65, -170, 6
    mx, my, hpx = 600, 244, 154
    shapes = g.polar_map(lat, lng, half, (200, 90, 800, 360), mx, my, hpx, None, None)
    assert not any("var(" in s and "style=" in s for s in shapes)   # resvg can't resolve inline var()
    img = _raster(g, shapes, theme)
    t = g.LIGHT if theme == "light" else g.DARK
    f = g.ortho(lat, lng, mx, my, hpx / math.radians(half))

    def at(lo, la):
        x, y, _ = f(lo, la)
        return img.getpixel((round(x), round(y)))

    def near(px, colour):
        return all(abs(a - b) <= 6 for a, b in zip(px, _hex(colour)))

    assert near(at(-170, 65), t["sea"])         # the pin, in the strait between the blocks
    assert near(at(-176, 64), t["land"])        # Chukotka, west of the line in data terms
    assert near(at(177, 68), t["land"])         # ...and its part east of +180°
    assert near(at(-160, 63), t["land"])        # Alaska
    assert near(at(175.5, 64.5), t["lake"])     # the lake stays a hole in the land


def test_polar_map_has_no_seam_along_the_line(synthetic) -> None:
    g = synthetic
    shapes = g.polar_map(65, -178, 3, (200, 90, 800, 360), 600, 244, 154, None, None)
    land = [s for s in shapes if 'class="ld"' in s]
    assert len(land) == 1
    # one subpath per land mass in view (Chukotka block, Alaska block): the seam would add a third
    assert land[0].count("M") <= 2


def test_topo_keeps_a_country_with_an_emptied_polygon(tmp_path) -> None:
    """sane-topojson empties the polygons it cut at 180° ([]); _topo used to raise and drop the country."""
    import json

    from obsidian_mcp.hero import generator as g

    topo = {"type": "Topology", "arcs": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
            "objects": {"countries": {"type": "GeometryCollection", "geometries": [
                {"type": "MultiPolygon", "id": "RUS", "properties": {"ct": [0.5, 0.5]}, "arcs": [[], [[0]]]}]}}}
    fn = tmp_path / "t.json"
    fn.write_text(json.dumps(topo))
    (got,) = g._topo(str(fn))
    assert got.area == pytest.approx(1)
    patched = box(-1, -1, 2, 2)
    (got,) = g._topo(str(fn), patch=lambda o: patched if o["id"] == "RUS" else None)
    assert got is patched


def test_polar_map_centred_on_lng_0_has_no_seam_at_180(synthetic, monkeypatch) -> None:
    """A pole view centred on lng 0 spans exactly ±180°, so qv() shifts nothing: the land Natural Earth
    splits at 180° must still be joined, or its coast stroke draws the 180th meridian."""
    g = synthetic
    # The far piece stops a hair short of -180°: float noise at the split that make_valid would not catch.
    layers = {**g.NE10, "land": [(box(150, -90, 180, -70), {}), (box(-180 + 1e-7, -90, -150, -70), {})],
              "lakes": [], "rivers": [], "adm0": []}
    monkeypatch.setattr(g, "NE10", layers)
    monkeypatch.setattr(g, "TREES", {k: STRtree([x for x, _ in v]) for k, v in layers.items()})
    shapes = g.polar_map(-89.5, 0, 8, (200, 90, 800, 360), 600, 244, 154, None, None)
    (land,) = [s for s in shapes if 'class="ld"' in s]
    assert land.count("M") == 1


def test_world_scale_inset_draws_the_world_once(synthetic) -> None:
    """At half 20 the inset view is ~700° wide; qv() would fetch two copies of the world (South Pacific Gyre:
    186 KB, 5 minutes). It is clamped to one world round the pin, and keeps only land and borders."""
    g = synthetic
    out = g.inset(-48.88, -123.39, 804, 106, 180, 112, 140, box(-160, -60, -90, -38), False)
    lands = re.findall(r'<path class="ld" d="([^"]*)"', out)
    assert sum(d.count("M") for d in lands) <= 3      # three land blocks in the synthetic world, each once
    assert 'class="rv"' not in out and 'class="lk"' not in out
    assert len(re.findall(r'<path class="b0"', out)) <= 1   # borders in one path
    # an ordinary inset (half_r 7) is untouched: rivers and lakes stay
    near = g.inset(65, -170, 804, 106, 180, 112, 7, box(-180, 62, -160, 68), False)
    assert 'class="rv"' in near
