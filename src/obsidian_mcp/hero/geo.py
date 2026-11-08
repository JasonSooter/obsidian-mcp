"""Geometry for place heroes across the 180° line and near the poles.

Pure functions on shapely geometries; no map data, so they are unit-tested on
their own (tests/test_hero_geo.py).

Natural Earth splits every feature at ±180°, so the data itself never wraps.
What breaks is a *view* that reaches past ±180°: a box from -182° to -158°
finds nothing on the far side of the line, where the data sits at +178°. The
fix is one coordinate convention, "continuous longitude": longitudes measured
from the view's centre, allowed past ±180° (-182° means 178°E). Features are
fetched from each copy of the world the view overlaps and shifted by 360° into
that range, so a view's geometry is always one continuous piece.

The polar view draws an orthographic globe. Geometry is clipped on the sphere
to a cap that stays in the near hemisphere *before* projecting (so no point is
ever behind the globe), then projected, then clipped to the panel in pixels.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator

import numpy as np
import shapely
from shapely.affinity import translate
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

WORLD = box(-180, -90, 180, 90)


def crosses(area: BaseGeometry) -> bool:
    """Whether an area in continuous longitude reaches past ±180°."""
    x0, _, x1, _ = area.bounds
    return x0 < -180 or x1 > 180


def world_copies(area: BaseGeometry) -> Iterator[tuple[BaseGeometry, float]]:
    """(part of `area` inside [-180, 180], shift) for each copy of the world it overlaps.

    The part is in data coordinates; adding `shift` to its longitudes puts it
    back where it was in `area`. An area inside [-180, 180] yields itself once,
    shift 0.
    """
    if not crosses(area):
        yield area, 0.0
        return
    x0, _, x1, _ = area.bounds
    for k in range(math.floor((x0 + 180) / 360), math.floor((x1 + 180) / 360) + 1):
        s = 360.0 * k
        part = translate(area, -s, 0).intersection(WORLD)
        if not part.is_empty:
            yield part, s


def wrap_lng(x: float, center: float) -> float:
    """`x` moved by whole turns to within 180° of `center` (continuous longitude)."""
    return center + (x - center + 180.0) % 360.0 - 180.0


def dlng(a: float, b: float) -> float:
    """Signed shortest longitude difference a - b, in (-180, 180]."""
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def unwrap(geom: BaseGeometry | None, center: float) -> BaseGeometry | None:
    """Shift each part of a geometry by whole turns to sit near `center`, and merge parts.

    A place outline from Nominatim that straddles 180° (Taveuni, Chukotka) comes
    back as parts on both sides of the line; in a view centred near the line,
    the far part must move by 360°. Returns the input object itself when no
    part moves, so views away from the line are untouched.
    """
    if geom is None or geom.is_empty:
        return geom
    parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    moved, any_moved = [], False
    for p in parts:
        c = p.centroid.x
        s = wrap_lng(c, center) - c
        if abs(s) > 1e-9:
            any_moved = True
            p = translate(p, s, 0)
        moved.append(p)
    if not any_moved:
        return geom
    merged = shapely.unary_union(moved)
    return merged if merged.is_valid else merged.buffer(0)


def cap(lat: float, lng: float, radius_deg: float, n: int = 180) -> Polygon:
    """The spherical cap within `radius_deg` of (lat, lng), as a polygon in continuous longitude.

    radius_deg must be under 90 so the cap is in one hemisphere. A cap that
    holds a pole becomes the band from its edge to that pole, across all 360°
    of longitude (lng - 180 to lng + 180).
    """
    if not 0 < radius_deg < 90:
        raise ValueError("cap radius must be in (0, 90) degrees")
    p1, d = math.radians(lat), math.radians(radius_deg)
    if radius_deg < 90 - abs(lat):
        t = np.linspace(0, 2 * math.pi, n, endpoint=False)
        p2 = np.arcsin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * np.cos(t))
        l2 = np.arctan2(np.sin(t) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * np.sin(p2))
        return Polygon(np.column_stack([lng + np.degrees(l2), np.degrees(p2)]))
    # Holds a pole: each meridian leaves the cap once, on the side away from the pole.
    north = lat > 0
    lam = np.linspace(-math.pi, math.pi, n + 1)
    A, B, C = math.sin(p1), math.cos(p1) * np.cos(lam), math.cos(d)
    rr = np.hypot(A, B)
    base, spread = np.arctan2(A, B), np.arccos(np.clip(C / rr, -1, 1))
    phi = base - spread if north else base + spread
    phi = np.clip(np.degrees(phi), -90, 90)
    edge = np.column_stack([lng + np.degrees(lam), phi])
    pole = 90.0 if north else -90.0
    ring = [*map(tuple, edge), (lng + 180.0, pole), (lng - 180.0, pole)]
    poly = Polygon(ring)
    return poly if poly.is_valid else poly.buffer(0)


def ortho_xy(lat0: float, lng0: float, cx: float, cy: float, r: float) -> Callable[[np.ndarray], np.ndarray]:
    """Vectorised orthographic projection, (N, 2) lon/lat in degrees to (N, 2) SVG pixels (y down).

    Matches generator.ortho() for points on the near side.
    """
    la0, lo0 = math.radians(lat0), math.radians(lng0)
    s0, c0 = math.sin(la0), math.cos(la0)

    def f(c: np.ndarray) -> np.ndarray:
        lo, la = np.radians(c[:, 0]) - lo0, np.radians(c[:, 1])
        x = np.cos(la) * np.sin(lo)
        y = c0 * np.sin(la) - s0 * np.cos(la) * np.cos(lo)
        return np.column_stack([cx + x * r, cy - y * r])

    return f


def project(geom: BaseGeometry, f: Callable[[np.ndarray], np.ndarray], frame: BaseGeometry) -> BaseGeometry:
    """Project a (cap-clipped) geometry to pixels and clip it to the frame, in pixels."""
    if geom.is_empty:
        return geom
    out = shapely.transform(geom, f)
    if not out.is_valid:
        out = shapely.make_valid(out)
    return out.intersection(frame)


def oriented(geom: BaseGeometry) -> BaseGeometry:
    """Polygons with exteriors counter-clockwise and holes clockwise (in pixel space).

    With SVG's default nonzero fill rule, a hole only stays open when it winds
    the other way from its exterior; this makes that true whatever the input.
    """
    if geom.geom_type == "Polygon":
        return orient(geom, 1.0)
    if geom.geom_type == "MultiPolygon":
        return MultiPolygon([orient(p, 1.0) for p in geom.geoms])
    if hasattr(geom, "geoms"):
        return shapely.GeometryCollection([oriented(g) for g in geom.geoms])
    return geom


def areas(geom: BaseGeometry) -> list[Polygon]:
    """The polygons in a geometry (dropping lines and points an intersection may leave)."""
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in areas(g)]
    return []


def lines(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "LinearRing":
        return [LineString(geom.coords)]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in lines(g)]
    return []


def max_lng_jump(geom: BaseGeometry) -> float:
    """Largest longitude step between neighbouring vertices: >180 means a ring or line wraps the wrong way."""
    worst = 0.0
    parts: Iterable[BaseGeometry] = geom.geoms if hasattr(geom, "geoms") else [geom]
    for p in parts:
        rings = [p.exterior, *p.interiors] if p.geom_type == "Polygon" else [p]
        for r in rings:
            x = np.asarray(r.coords)[:, 0]
            if len(x) > 1:
                worst = max(worst, float(np.abs(np.diff(x)).max()))
    return worst

