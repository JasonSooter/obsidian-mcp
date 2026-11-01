#!/usr/bin/env python3
"""Place hero generator (Nord palette, 1200x630 SVG, dark + light in one file).

Run by the server in a subprocess, one batch at a time (see hero/jobs.py); not
imported by it. Adapted from the "v2" generator a scheduled routine kept in an
Obsidian vault and ran in its own sandbox. Differences from that copy:

- configuration comes from the environment (HERO_DATA, HERO_USER_AGENT), and
  no site or personal details are baked in;
- network access is urllib, not curl, and setup downloads the data tarballs
  over HTTPS itself instead of running npm or pip (the image has neither);
- per-job overrides: `half` (alias `zoom`), `pin_label: false`, `region: true`;
- fixes: a pin at either pole, regions with no outline drawn at street scale, a
  neighbouring place labelled at the pin, contested-area subtitles;
- Wikipedia is asked at most until it throttles once per batch, and the result
  reports where landmarks came from.

Map content: Natural Earth 10m rivers with names, lakes, roads, railways, urban
areas, provinces, deserts and mountain ranges with names, peaks, seas; a
locator globe, a regional inset, a scale bar; subtitles from reverse geocoding.

Themes: one SVG holds both. It renders dark by default; loaded as `<file>.svg#light`
it renders light (CSS :target), and `#dark` forces dark.

Setup (once; idempotent):  python -m obsidian_mcp.hero.generator --setup
  - all-the-cities@3.1.0 and sane-topojson@4.0.0 data tarballs from the npm
    registry (only data files are extracted; nothing in them is executed)
  - Natural Earth 10m GeoJSON from raw.githubusercontent.com/nvkelso/natural-earth-vector (public domain)
  - builds $HERO_DATA/ne10.pkl (shapely geometries) and $HERO_DATA/cities.json
Usage: python -m obsidian_mcp.hero.generator jobs.json OUTDIR
jobs.json: list of {"name", "lat", "lng", "icon" (book|newspaper|podcast|video),
  "confidence" (null|documented|inferred|best-guess), "osm" (e.g. "W62208778" or null),
  "query" (Nominatim search text for the place's outline, or null),
  "parent" (Nominatim search text for the parent place, or null),
  optional: "subtitle", "aliases" (list), "half"/"zoom" (view half-height in degrees),
  "pin_label" (false: no place label at the pin), "region" (true: frame as a region)}
Writes OUTDIR/<index>.svg and prints one JSON line per job, in order:
  {index, name, bytes, subtitle, pin_label, outline, lens, landmarks, secs, ...} or {index, name, error}.
Output aims at ~32 KB per SVG (coarser simplification on retry) and may exceed it.
Network at run time: nominatim.openstreetmap.org (1 req/s) and en.wikipedia.org/w/api.php (geosearch),
with HERO_USER_AGENT as the User-Agent (Nominatim's usage policy requires one that identifies you).
"""
import json, math, os, sys, time, urllib.parse, urllib.request, urllib.error, html, warnings, pickle, unicodedata, functools, re
import io, tarfile, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("HERO_DATA") or os.path.join(HERE, "ne")
NE10_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
NPM = "https://registry.npmjs.org/"
UA = os.environ.get("HERO_USER_AGENT") or "obsidian-mcp-hero/1.0 (+https://github.com/JasonSooter/obsidian-mcp)"
NE10_FILES = ["ne_10m_rivers_lake_centerlines", "ne_10m_rivers_europe", "ne_10m_rivers_north_america", "ne_10m_lakes",
              "ne_10m_roads", "ne_10m_railroads", "ne_10m_urban_areas", "ne_10m_admin_1_states_provinces_lines",
              "ne_10m_admin_0_boundary_lines_land", "ne_10m_coastline", "ne_10m_land", "ne_10m_geography_regions_polys",
              "ne_10m_geography_regions_points", "ne_10m_geography_regions_elevation_points",
              "ne_10m_geography_marine_polys", "ne_10m_admin_0_countries", "ne_10m_airports", "ne_10m_ports"]


def _cities_from_pbf(fn):
    """Decode all-the-cities' cities.pbf (pure Python; no package code is run)."""
    buf = open(fn, "rb").read(); pos = 0; n = len(buf); lon = lat = 0; out = []
    def varint(p):
        r = sh = 0
        while True:
            b = buf[p]; p += 1; r |= (b & 0x7F) << sh; sh += 7
            if b < 0x80: return r, p
    while pos < n:
        ln, pos = varint(pos); end = pos + ln; c = {"name": "", "cc": "", "pop": 0}
        while pos < end:
            key, pos = varint(pos); tag, wt = key >> 3, key & 7
            if wt == 0:
                v, pos = varint(pos)
                if tag == 9: c["pop"] = v
                elif tag in (1, 10, 11):
                    v = (v >> 1) ^ -(v & 1)
                    if tag == 10: lon += v
                    elif tag == 11: lat += v
            elif wt == 2:
                l, pos = varint(pos); t_ = buf[pos:pos + l].decode("utf-8", "replace"); pos += l
                if tag == 2: c["name"] = t_
                elif tag == 3: c["cc"] = t_
            elif wt == 5: pos += 4
            elif wt == 1: pos += 8
        out.append([c["name"], round(lat / 1e5, 4), round(lon / 1e5, 4), c["pop"], c["cc"]])
    return out


def _build_ne10(src, dst):
    from shapely.geometry import shape
    def feats(f):
        for ft in json.load(open(os.path.join(src, f + ".geojson")))["features"]:
            try:
                g = shape(ft["geometry"])
            except Exception:
                continue
            if g.is_empty: continue
            if not g.is_valid: g = g.buffer(0)
            yield g, ft["properties"]
    P = lambda p, *ks: next((p.get(k) for k in ks if p.get(k) not in (None, "")), None)
    L = {}
    L["land"] = [(g, {}) for g, p in feats("ne_10m_land")]
    L["coast"] = [(g, {}) for g, p in feats("ne_10m_coastline")]
    L["lakes"] = [(g, {"n": P(p, "name"), "r": p.get("scalerank", 9)}) for g, p in feats("ne_10m_lakes")]
    L["rivers"] = []
    for f in ("ne_10m_rivers_lake_centerlines", "ne_10m_rivers_europe", "ne_10m_rivers_north_america"):
        L["rivers"] += [(g, {"n": P(p, "name", "name_en"), "r": p.get("scalerank", 9) or 9}) for g, p in feats(f)
                        if p.get("featurecla", "River") != "Lake Centerline"]
    L["roads"] = [(g, {"t": p.get("type"), "r": p.get("scalerank", 9)}) for g, p in feats("ne_10m_roads")
                  if p.get("type") not in ("Ferry Route", "Ferry, seasonal")]
    L["rail"] = [(g, {"r": p.get("scalerank", 9)}) for g, p in feats("ne_10m_railroads")]
    L["urban"] = [(g, {}) for g, p in feats("ne_10m_urban_areas")]
    L["adm1"] = [(g, {}) for g, p in feats("ne_10m_admin_1_states_provinces_lines")]
    L["adm0"] = [(g, {}) for g, p in feats("ne_10m_admin_0_boundary_lines_land")]
    L["regions"] = [(g, {"n": P(p, "LABEL", "NAME"), "c": p.get("FEATURECLA"), "r": p.get("SCALERANK", 9)})
                    for g, p in feats("ne_10m_geography_regions_polys")]
    L["regpts"] = [(g, {"n": P(p, "label", "name"), "c": p.get("featurecla"), "r": p.get("scalerank", 9)})
                   for g, p in feats("ne_10m_geography_regions_points")]
    L["peaks"] = [(g, {"n": P(p, "label", "name"), "e": p.get("elevation"), "c": p.get("featurecla"), "r": p.get("scalerank", 9)})
                  for g, p in feats("ne_10m_geography_regions_elevation_points")]
    L["marine"] = [(g, {"n": P(p, "label", "name"), "c": p.get("featurecla"), "r": p.get("scalerank", 9)})
                   for g, p in feats("ne_10m_geography_marine_polys")]
    L["countries"] = [(g, {"n": P(p, "NAME", "ADMIN"), "x": p.get("LABEL_X"), "y": p.get("LABEL_Y"), "r": p.get("LABELRANK", 5)})
                      for g, p in feats("ne_10m_admin_0_countries")]
    L["airports"] = [(g, {"n": P(p, "name"), "t": p.get("type"), "r": p.get("scalerank", 9)}) for g, p in feats("ne_10m_airports")]
    L["ports"] = [(g, {"n": P(p, "name"), "r": p.get("scalerank", 9)}) for g, p in feats("ne_10m_ports")]
    pickle.dump(L, open(dst, "wb"), protocol=pickle.HIGHEST_PROTOCOL)


def _fetch(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _write_atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".dl.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp): os.unlink(tmp)
        raise


def _npm_member(pkg, version, member):
    """One file out of an npm package tarball: a plain HTTPS download, never `npm` (nothing is run)."""
    tgz = _fetch(f"{NPM}{pkg}/-/{pkg}-{version}.tgz")
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as t:
        f = t.extractfile(f"package/{member}")
        if f is None: raise RuntimeError(f"{pkg}@{version} has no {member}")
        return f.read()


def setup():
    """Download and build everything under DATA. Idempotent: present files are kept."""
    os.makedirs(DATA, exist_ok=True)
    src = os.path.join(DATA, "ne10src"); os.makedirs(src, exist_ok=True)
    for name in ("world_50m.json", "world_110m.json"):
        fn = os.path.join(DATA, name)
        if not os.path.exists(fn):
            _write_atomic(fn, _npm_member("sane-topojson", "4.0.0", f"dist/{name}"))
    cities = os.path.join(DATA, "cities.json")
    if not os.path.exists(cities):
        pbf = os.path.join(DATA, "cities.pbf")
        _write_atomic(pbf, _npm_member("all-the-cities", "3.1.0", "cities.pbf"))
        _write_atomic(cities, json.dumps(_cities_from_pbf(pbf)).encode())
    for f in NE10_FILES:
        fn = os.path.join(src, f + ".geojson")
        if not os.path.exists(fn) or os.path.getsize(fn) < 1000:
            _write_atomic(fn, _fetch(NE10_BASE + f + ".geojson", timeout=600))
    pkl = os.path.join(DATA, "ne10.pkl")
    if not os.path.exists(pkl):
        tmp = pkl + ".tmp"
        _build_ne10(src, tmp)
        os.replace(tmp, pkl)
    return sorted(os.listdir(DATA))


warnings.filterwarnings("ignore")
from shapely.geometry import shape, box, LineString, MultiLineString, Polygon, MultiPolygon, Point
from shapely.strtree import STRtree

W, H = 1200, 630
FONT = "'Space Grotesk','Helvetica Neue',Helvetica,Arial,sans-serif"
MONO = "'JetBrains Mono',Menlo,Consolas,monospace"
# Theme variables: dark (default) and light (Nord Snow Storm)
DARK = dict(bg="#2e3440", g1="#5a7486", g2="#5b4a5e", grid="#ffffff", sea="#232a34", land="#3b4252", par="#434c5e",
            coast="#4c566a", b0="#7b88a1", b1="#4c566a", water="#5e81ac", wl="#88c0d0", lake="#2b3a4d",
            urban="#81a1c1", road="#d8dee9", rail="#a3adbf", desert="#ebcb8b", mount="#a3be8c", wet="#8fbcbb",
            t1="#eceff4", t2="#d8dee9", t3="#9aa5b8", card="#353c48", cardb="#4c566a", chip="#3b4252",
            frame="#4c566a", coord="#81a1c1", shadow=".45", globe="#2b3442", gland="#4c566a", lm="#d08770")
LIGHT = dict(bg="#eceff4", g1="#b8cad8", g2="#d9c6d6", grid="#2e3440", sea="#c6d4e2", land="#f6f8fb", par="#e3e8f0",
             coast="#9fb0c4", b0="#7b88a1", b1="#b4bfcd", water="#5e81ac", wl="#4c6f96", lake="#b3c8dc",
             urban="#5e81ac", road="#4c566a", rail="#7b88a1", desert="#c99a3c", mount="#6f8f55", wet="#5f9e9c",
             t1="#2e3440", t2="#3b4252", t3="#5d6779", card="#ffffff", cardb="#d1d8e2", chip="#e5e9f0",
             frame="#c3ccd8", coord="#5e81ac", shadow=".16", globe="#dde5ee", gland="#aab6c6", lm="#b8603f")
ACC = {"dark": ("#b48ead", "#88c0d0"), "light": ("#9a6d93", "#3f7f99")}   # (documented, guess)
ICON = {"book": ("#a3be8c", "#5e81ac", "Book"), "newspaper": ("#ebcb8b", "#d08770", "Article"),
        "podcast": ("#b48ead", "#5e81ac", "Podcast"), "video": ("#bf616a", "#b48ead", "Video")}


def _topo(fn):
    t = json.load(open(fn)); tr = t.get("transform"); arcs = []
    for a in t["arcs"]:
        x = y = 0; pts = []
        for p in a:
            if tr:
                x += p[0]; y += p[1]
                pts.append((x * tr["scale"][0] + tr["translate"][0], y * tr["scale"][1] + tr["translate"][1]))
            else:
                pts.append(tuple(p[:2]))
        arcs.append(pts)
    def line(ids):
        out = []
        for i in ids:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(a if not out else a[1:])
        return out
    def geom(o):
        ty = o.get("type")
        try:
            if ty == "Polygon": return Polygon(line(o["arcs"][0]), [line(r) for r in o["arcs"][1:]])
            if ty == "MultiPolygon": return MultiPolygon([Polygon(line(p[0]), [line(r) for r in p[1:]]) for p in o["arcs"]])
        except Exception:
            return None
        return None
    gs = t["objects"]["countries"]["geometries"]
    out = [g for g in (geom(o) for o in gs) if g is not None and not g.is_empty]
    return [g if g.is_valid else g.buffer(0) for g in out]


# Loaded by load(), not at import: tests import the pure helpers without the ~GB of map data.
NE10 = TREES = C110 = CITIES = None


def load():
    global NE10, TREES, C110, CITIES, LMC
    with open(os.path.join(DATA, "ne10.pkl"), "rb") as f:
        NE10 = pickle.load(f)
    TREES = {k: STRtree([g for g, _ in v]) for k, v in NE10.items()}
    C110 = _topo(os.path.join(DATA, "world_110m.json"))
    with open(os.path.join(DATA, "cities.json")) as f:
        CITIES = json.load(f)  # [name, lat, lng, pop, cc]
    try:
        with open(CACHE_FN) as f:
            LMC = json.load(f)
    except Exception:
        LMC = {}


@functools.lru_cache(maxsize=None)
def C50():
    return _topo(os.path.join(DATA, "world_50m.json"))



def q(layer, area):
    return [NE10[layer][i] for i in TREES[layer].query(area)]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _get(u):
    """GET JSON with retries; None after four failures. Paced: Nominatim allows 1 request/s."""
    for i in range(4):
        try:
            body = _fetch(u, timeout=30).decode("utf-8")
        except (urllib.error.URLError, OSError, ValueError):
            body = ""
        time.sleep(1.1 if "nominatim" in u else 0.2)
        try:
            return json.loads(body)
        except Exception:
            time.sleep(3 * (i + 1))
    return None


NOM = "https://nominatim.openstreetmap.org/"
@functools.lru_cache(maxsize=None)
def nominatim(qs=None, osm=None):
    p = dict(format="json", polygon_geojson=1, polygon_threshold=0.0005, addressdetails=1, **{"accept-language": "en"})
    if osm:
        j = _get(NOM + "lookup?" + urllib.parse.urlencode(dict(p, osm_ids=osm)))
    elif qs:
        j = _get(NOM + "search?" + urllib.parse.urlencode(dict(p, q=qs, limit=1)))
    else:
        return None
    return j[0] if isinstance(j, list) and j else None


@functools.lru_cache(maxsize=None)
def reverse(lat, lng, zoom=10):
    j = _get(NOM + "reverse?" + urllib.parse.urlencode(dict(format="json", lat=lat, lon=lng, zoom=zoom, addressdetails=1,
                                                            polygon_geojson=1, polygon_threshold=0.0005, **{"accept-language": "en"})))
    return j if isinstance(j, dict) and "address" in j else None


def poly(rec):
    if rec and (rec.get("geojson") or {}).get("type") in ("Polygon", "MultiPolygon"):
        g = shape(rec["geojson"])
        return g if g.is_valid else g.buffer(0)
    return None


class Proj:
    def __init__(s, lat, lng, half, cx, cy, hpx):
        s.lat, s.lng, s.k = lat, lng, max(math.cos(math.radians(lat)), 0.2)
        s.sc = hpx / half; s.cx, s.cy = cx, cy
    def __call__(s, x, y):
        return s.cx + (x - s.lng) * s.k * s.sc, s.cy - (y - s.lat) * s.sc
    def view(s, wpx, hpx):
        return box(s.lng - wpx / (s.k * s.sc), max(-89.9, s.lat - hpx / s.sc), s.lng + wpx / (s.k * s.sc), min(89.9, s.lat + hpx / s.sc))


TOL = [1.2]
def _ring(pts, z):
    out, px, py = [], None, None
    for x, y in pts:
        x, y = round(x), round(y)
        if px is None:
            out.append(f"M{x} {y}")
        elif (x, y) != (px, py):
            out.append(f"l{x-px} {y-py}")
        px, py = x, y
    if len(out) < 2: return ""
    return "".join(out).replace(" -", "-") + ("z" if z else "")


def d_of(geom, P, view, closed=True, tol=None):
    if geom is None or geom.is_empty or not geom.intersects(view):
        return ""
    geom = geom.intersection(view).simplify((tol or TOL[0]) / P.sc)
    out = []
    def walk(g):
        t = g.geom_type
        if t == "Polygon":
            out.append(_ring([P(*c) for c in g.exterior.coords], True)); [out.append(_ring([P(*c) for c in r.coords], True)) for r in g.interiors]
        elif t == "LineString":
            out.append(_ring([P(*c) for c in g.coords], False))
        elif hasattr(g, "geoms"):
            [walk(x) for x in g.geoms]
    walk(geom)
    return "".join(out)


class Boxes:
    def __init__(s, bounds): s.b = []; s.bounds = bounds
    def fits(s, r):
        bx0, by0, bx1, by1 = s.bounds
        if r[0] < bx0 or r[2] > bx1 or r[1] < by0 or r[3] > by1: return False
        return not any(not (r[2] < q[0] or r[0] > q[2] or r[3] < q[1] or r[1] > q[3]) for q in s.b)
    def add(s, r): s.b.append(r)
    def take(s, r):
        if s.fits(r): s.add(r); return True
        return False


def tw(text, fs, k=0.56, ls=0.0):
    return len(text) * (fs * k + ls)


def basemap(P, view, half, A, boxes, lite):
    """Returns (shapes, labels). A = SVG fragments list for defs (patterns use classes)."""
    S, Lb = [], []
    va = view.area
    path = lambda d, c: S.append(f'<path class="{c}" d="{d}"/>') if d else None
    # land (10m below ~3 degrees half-height, 50m-ish via 10m simplified above)
    for g, _ in q("land", view):
        path(d_of(g, P, view), "ld")
    # regions: deserts / mountains / wetlands get a texture
    regs = []
    for g, p in q("regions", view):
        c = (p.get("c") or "").lower()
        cls = "ds" if c in ("desert",) else "mt" if c in ("range/mtn", "foothills", "plateau") else "wt" if c in ("wetlands", "delta") else None
        vis = g.intersection(view)
        if vis.is_empty or (p.get("c") or "").lower() == "continent": continue
        if cls: path(d_of(vis, P, view, tol=TOL[0] * 2), cls)
        regs.append((vis.area / va, vis, p))
    if half < 3 and lite < 3:
        for g, _ in q("urban", view):
            path(d_of(g, P, view), "ur")
    for g, p in q("lakes", view):
        if half > 4 and p["r"] > 6: continue
        path(d_of(g, P, view), "lk")
    rivers = []
    for g, p in q("rivers", view):
        if half > 6 and p["r"] > 4: continue
        if half > 2.5 and p["r"] > 7: continue
        d = d_of(g, P, view, False)
        if d:
            w = 2.2 if p["r"] <= 3 else 1.7 if p["r"] <= 6 else 1.2
            S.append(f'<path class="rv" stroke-width="{w}" d="{d}"/>')
            rivers.append((g, p))
    if half < 5 and lite < 2:
        for g, _ in q("adm1", view):
            path(d_of(g, P, view, False), "b1")
    for g, _ in q("adm0", view):
        path(d_of(g, P, view, False), "b0")
    if half < 2 and lite < 1:
        for g, p in q("rail", view):
            path(d_of(g, P, view, False), "rr")
    if half < 6:
        minor = [] if lite else None
        for g, p in q("roads", view):
            major = p.get("t") in ("Major Highway", "Beltway")
            if not major and (half > 2.5 or lite >= 1): continue
            if lite >= 3: continue
            path(d_of(g, P, view, False), "rm" if major else "rd")
    return S, regs, rivers


def labels_regions(P, view, regs, boxes, half):
    out = []
    for frac, vis, p in sorted(regs, key=lambda r: (r[2]["r"], -r[0])):
        n = p.get("n")
        if not n or frac < 0.04 or (p.get("c") or "").lower() in ("continent", "pen/cape") and frac > 0.5: continue
        rp = vis.representative_point()
        x, y = P(rp.x, rp.y)
        t = n.upper(); fs = 13
        w = tw(t, fs, 0.62, 2.6)
        r = (x - w / 2, y - 12, x + w / 2, y + 4)
        if boxes.take(r):
            out.append(f'<text class="rl" x="{x:.0f}" y="{y:.0f}" text-anchor="middle">{html.escape(t)}</text>')
        if len(out) >= 3: break
    return out


def labels_rivers(P, view, rivers, boxes, A):
    out, seen = [], set()
    cand = []
    for g, p in rivers:
        n = p.get("n")
        if not n: continue
        vis = g.intersection(view)
        if vis.is_empty: continue
        lines = list(vis.geoms) if hasattr(vis, "geoms") else [vis]
        best = max((l for l in lines if l.geom_type == "LineString"), key=lambda l: l.length, default=None)
        if best is None: continue
        cand.append((best.length * P.sc, best, n))
    for L, best, n in sorted(cand, key=lambda c: -c[0]):
        if norm(n) in seen or L < 200: continue
        sm = best.simplify(6 / P.sc)
        pts = [P(*c) for c in sm.coords]
        if pts[0][0] > pts[-1][0]: pts = pts[::-1]
        mid = pts[len(pts) // 2]
        w = tw(n, 13, 0.55)
        if w > L * 0.7: continue
        r = (mid[0] - w / 2, mid[1] - 14, mid[0] + w / 2, mid[1] + 4)
        if not boxes.take(r): continue
        i = len(seen)
        A.append(f'<path id="rv{i}" d="{_ring(pts, False)}"/>')
        out.append(f'<text class="wl" dy="-4"><textPath href="#rv{i}" startOffset="50%" text-anchor="middle">{html.escape(n)}</textPath></text>')
        seen.add(norm(n))
        if len(seen) >= 3: break
    return out


def labels_points(P, view, boxes, half):
    out = []
    pk = sorted(q("peaks", view), key=lambda r: -(r[1].get("e") or 0))
    for g, p in pk[:4]:
        if not view.contains(g) or not p.get("n"): continue
        x, y = P(g.x, g.y)
        e = p.get("e")
        t = p["n"] + (f" {e:,} m".replace(",", " ") if e and e > 0 else "")
        w = tw(t, 12, 0.55) + 14
        if boxes.take((x - 6, y - 8, x + w, y + 8)):
            out.append(f'<path class="pk" d="M{x-5:.0f} {y+4:.0f}l5-9 5 9z"/><text class="pt" x="{x+9:.0f}" y="{y+4:.0f}">{html.escape(t)}</text>')
    for g, p in q("regpts", view):
        if not view.contains(g) or not p.get("n"): continue
        x, y = P(g.x, g.y); t = p["n"]; w = tw(t, 12, 0.55)
        if boxes.take((x - w / 2, y - 10, x + w / 2, y + 4)):
            out.append(f'<text class="pt" x="{x:.0f}" y="{y:.0f}" text-anchor="middle" font-style="italic">{html.escape(t)}</text>')
    va = view.area
    for g, p in sorted(q("marine", view), key=lambda r: r[1]["r"]):
        vis = g.intersection(view)
        if vis.is_empty or vis.area < va * 0.05 or not p.get("n"): continue
        rp = vis.representative_point(); x, y = P(rp.x, rp.y)
        t = p["n"]; w = tw(t, 14, 0.55, 1.5)
        if boxes.take((x - w / 2, y - 12, x + w / 2, y + 4)):
            out.append(f'<text class="ml" x="{x:.0f}" y="{y:.0f}" text-anchor="middle">{html.escape(t)}</text>')
    if half > 0.5:
        for g, p in sorted(q("countries", view), key=lambda r: r[1]["r"]):
            if p.get("x") is None or not p.get("n"): continue
            if not view.contains(Point(p["x"], p["y"])): continue
            x, y = P(p["x"], p["y"]); t = p["n"].upper(); w = tw(t, 13, 0.62, 3)
            if boxes.take((x - w / 2, y - 12, x + w / 2, y + 4)):
                out.append(f'<text class="cl" x="{x:.0f}" y="{y:.0f}" text-anchor="middle">{html.escape(t)}</text>')
    return out


def towns(P, view, half, boxes, exclude, settlement, maxn=10):
    x0, y0, x1, y1 = view.bounds
    minpop = 2000 if half < 0.15 else 8000 if half < 0.35 else 20000 if half < 0.8 else 80000 if half < 2 else 400000 if half < 6 else 2000000
    cand = sorted((c for c in CITIES if y0 < c[1] < y1 and x0 < c[2] < x1 and c[3] >= minpop), key=lambda c: -c[3])
    cx, cy = P(P.lng, P.lat)
    out, seen = [], set(exclude)
    for nm, la, ln, pop, cc in cand:
        if len(out) >= maxn: break
        k = norm(nm)
        if not k or k in seen: continue
        x, y = P(ln, la)
        if settlement and math.hypot(x - cx, y - cy) < 70: continue   # districts of the place itself
        big = pop >= (minpop * 8)
        fs = 15 if big else 13
        w = tw(nm, fs, 0.56) + 12
        r = (x - 5, y - fs, x + w, y + 6)
        if not boxes.take(r): continue
        seen.add(k)
        out.append(f'<circle class="dt" cx="{x:.0f}" cy="{y:.0f}" r="{3.4 if big else 2.6}"/>'
                   f'<text class="{"tb" if big else "tx"}" x="{x+8:.0f}" y="{y+5:.0f}">{html.escape(nm)}</text>')
    return out


def ortho(lat0, lng0, cx, cy, r):
    la0, lo0 = math.radians(lat0), math.radians(lng0)
    def f(lng, lat):
        la, lo = math.radians(lat), math.radians(lng)
        cosc = math.sin(la0) * math.sin(la) + math.cos(la0) * math.cos(la) * math.cos(lo - lo0)
        x = math.cos(la) * math.sin(lo - lo0)
        y = math.cos(la0) * math.sin(la) - math.sin(la0) * math.cos(la) * math.cos(lo - lo0)
        if cosc < 0:
            m = math.hypot(x, y) or 1; x, y = x / m, y / m
        return cx + x * r, cy - y * r, cosc >= 0
    return f


def globe(lat, lng, cx, cy, r, hl, view):
    f = ortho(max(-60, min(60, lat)), lng, cx, cy, r)
    out = [f'<circle class="gb" cx="{cx}" cy="{cy}" r="{r}"/>']
    def rings(g):
        polys = list(g.geoms) if hasattr(g, "geoms") else [g]
        for p in polys:
            pts = [f(x, y) for x, y in p.exterior.coords]
            if not any(v for _, _, v in pts): continue
            ls = LineString([(a, b) for a, b, _ in pts]).simplify(0.9)
            if ls.length < 6: continue
            yield _ring(list(ls.coords), True)
    for g in C110:
        d = "".join(rings(g))
        if d: out.append(f'<path class="{"gh" if hl is not None and g.intersects(hl) else "gl"}" d="{d}"/>')
    x, y, v = f(lng, lat)
    vb = view.bounds
    (ax, ay, _), (bx, by, _) = f(vb[0], vb[3]), f(vb[2], vb[1])
    if abs(bx - ax) > 7 and abs(by - ay) > 5:
        out.append(f'<rect class="gr" x="{min(ax,bx):.0f}" y="{min(ay,by):.0f}" width="{abs(bx-ax):.0f}" height="{abs(by-ay):.0f}"/>')
    out.append(f'<circle class="gp" cx="{x:.0f}" cy="{y:.0f}" r="3.2"/>')
    return f'<g clip-path="url(#gc)"><clipPath id="gc"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath>{"".join(out)}</g><circle class="go" cx="{cx}" cy="{cy}" r="{r}"/>'


def scalebar(P, x, y):
    kmpx = 111.32 / P.sc
    target = 110 * kmpx
    n = 10 ** math.floor(math.log10(target))
    for m in (5, 2, 1):
        if m * n <= target: v = m * n; break
    px = v / kmpx
    lab = f"{v:g} km" if v >= 1 else f"{v*1000:g} m"
    return (f'<path class="sb" d="M{x} {y-5}v5h{px:.0f}v-5"/>'
            f'<text class="st" x="{x+px+8:.0f}" y="{y+1}">{lab}</text>')


def css(dark, light, guess):
    def vars_(t, a):
        acc = ACC[a][1] if guess else ACC[a][0]
        return ";".join(f"--{k}:{v}" for k, v in t.items()) + f";--acc:{acc}"
    rules = (".pb{fill:var(--bg)}.pg{fill:url(#g)}.ps{fill:var(--sea)}.ld{fill:var(--land);stroke:var(--coast);stroke-width:1}.pa{fill:var(--par);fill-opacity:.55;stroke:var(--water);stroke-opacity:.5;stroke-width:1.4;stroke-dasharray:6 5}"
             ".cs{fill:none;stroke:var(--coast);stroke-width:1}.b0{fill:none;stroke:var(--b0);stroke-width:1.4;stroke-dasharray:7 4 2 4;stroke-opacity:.75}"
             ".b1{fill:none;stroke:var(--b1);stroke-width:1;stroke-dasharray:3 3}.rv{fill:none;stroke:var(--water);stroke-linecap:round;stroke-linejoin:round}"
             ".lk{fill:var(--lake);stroke:var(--water);stroke-opacity:.6}.ur{fill:var(--urban);fill-opacity:.2;stroke:var(--urban);stroke-opacity:.35}"
             ".rd{fill:none;stroke:var(--road);stroke-opacity:.28;stroke-width:1}.rm{fill:none;stroke:var(--road);stroke-opacity:.45;stroke-width:1.8;stroke-linejoin:round}"
             ".rr{fill:none;stroke:var(--rail);stroke-opacity:.5;stroke-width:1.2;stroke-dasharray:2 3}"
             ".ds{fill:url(#pd)}.mt{fill:url(#pm)}.wt{fill:url(#pw)}.pdd{fill:var(--desert);fill-opacity:.45}.pmm{fill:none;stroke:var(--mount);stroke-opacity:.45;stroke-width:1.1}"
             ".pww{fill:none;stroke:var(--wet);stroke-opacity:.4}.mg{fill:none;stroke:var(--water);stroke-opacity:.07}.gg{fill:none;stroke:var(--grid);stroke-opacity:.035}"
             ".ol{fill:var(--acc);fill-opacity:.24;stroke:var(--acc);stroke-width:2.2}.dt{fill:var(--t2);fill-opacity:.8}"
             ".ap{stroke:var(--lm);stroke-width:2.2}.lm{fill:var(--lm);stroke:var(--sea);stroke-width:1}.lt{font-size:12px;fill:var(--t2)}"
             ".tx,.tb,.pt,.rl,.wl,.ml,.cl,.pl,.lt{paint-order:stroke;stroke:var(--sea);stroke-width:3px;stroke-opacity:.75;stroke-linejoin:round}"
             ".tx{font-size:13px;fill:var(--t2);fill-opacity:.8}.tb{font-size:15px;font-weight:600;fill:var(--t1);fill-opacity:.88}"
             ".pt{font-size:12px;fill:var(--t3)}.pk{fill:var(--t3)}.rl{font-size:13px;letter-spacing:2.6px;fill:var(--t3);font-style:italic;fill-opacity:.9}"
             ".wl{font-size:13px;fill:var(--wl);font-style:italic}.ml{font-size:14px;letter-spacing:1.5px;fill:var(--wl);font-style:italic}"
             ".cl{font-size:13px;letter-spacing:3px;fill:var(--t3);font-weight:600}.pl{font-size:16px;font-weight:600;fill:var(--t1)}"
             ".pin{fill:var(--acc);stroke:var(--t1);stroke-width:3}.ph{fill:url(#p)}.pc{stop-color:var(--acc)}"
             ".gb{fill:var(--globe)}.gl{fill:var(--gland)}.gh{fill:var(--acc)}.gr{fill:none;stroke:var(--t1);stroke-width:1.2}.gp{fill:var(--t1)}.go{fill:none;stroke:var(--frame);stroke-width:1.2}"
             ".sb{fill:none;stroke:var(--t3);stroke-width:1.5}.st{font:11px " + MONO.replace("'", "") + ";fill:var(--t3)}"
             ".lb{fill:var(--sea)}.lr{fill:none;stroke:var(--t3);stroke-opacity:.6;stroke-width:1.5}.ll{stroke:var(--t3);stroke-opacity:.5;stroke-dasharray:3 4}"
             ".pn{fill:var(--sea)}.fr{fill:none;stroke:var(--frame);stroke-opacity:.8}.cd{fill:var(--card);stroke:var(--cardb)}"
             ".t1{fill:var(--t1)}.t3{fill:var(--t3)}.ch{fill:var(--chip);stroke:var(--cardb)}.co{fill:var(--coord)}"
             ".s1{stop-color:var(--g1)}.s2{stop-color:var(--g2)}.s0{stop-color:var(--bg)}.sh{flood-opacity:var(--shadow)}")
    return (f"<style>#r{{{vars_(dark, 'dark')}}}#light:target~#r{{{vars_(light, 'light')}}}"
            f"#dark:target~#r{{{vars_(dark, 'dark')}}}{rules}</style>")


EVENT = re.compile(r"^\d{3,4}\b|\b(battles?|siege|fall of|bombings?|attacks?|escape|massacres?|offensive|raids?|riots?|protests?|shootings?|earthquake|incident|crash|uprising|campaign|operation|killings?|explosion|fire of|floods?|elections?|treaty|conference|summit|murders?|assassination|hijacking|strike|demonstrations?|disaster|stabbing|standoff|hostage|ambush|skirmish|clashes?|capture of|liberation of|sack of|occupation of)\b", re.I)
ADMIN = re.compile(r"\b(district|province|county|municipality|governorate|oblast|prefecture|parish|ward|constituency|electoral|census)\b", re.I)


CACHE_FN = os.path.join(DATA, "landmark_cache.json")
LMC = {}


def _save_cache():
    try:
        _write_atomic(CACHE_FN, json.dumps(LMC).encode())
    except Exception:
        pass


WIKI_PRE = {}
WIKI_DOWN = [False]   # set when Wikimedia throttles us: no more requests this batch; the cache retries next run
def wiki_near(lat, lng):
    """Geotagged English Wikipedia articles within 10 km (one request; None when Wikimedia throttles us)."""
    if WIKI_DOWN[0]:
        return None
    u = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(dict(
        action="query", format="json", generator="geosearch", ggscoord=f"{lat}|{lng}", ggsradius=10000,
        ggslimit=100, prop="coordinates|info", coprop="type", colimit="max"))
    try:
        r = _fetch(u, timeout=30).decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        r = ""
    time.sleep(0.5)
    try:
        j = json.loads(r)
    except Exception:
        WIKI_DOWN[0] = True
        return None
    out = []
    for p in ((j.get("query") or {}).get("pages") or {}).values():
        c = (p.get("coordinates") or [{}])[0]
        if "lat" not in c or c.get("type") == "event": continue
        t = p["title"]
        if EVENT.search(t) or ADMIN.search(t) or t.startswith(("List of", "Timeline")): continue
        out.append([t, float(c["lat"]), float(c["lon"]), p.get("length", 0), c.get("type")])
    return sorted(out, key=lambda r: -r[3])


NOM_CATS = ("attraction", "museum", "monument", "castle")
def nominatim_near(lat, lng, half):
    k = max(math.cos(math.radians(lat)), 0.2)
    vb = f"{lng - half * 2.4 / k},{lat + half},{lng + half * 2.4 / k},{lat - half}"
    out = []
    for i, cat in enumerate(NOM_CATS):
        j = _get(NOM + "search?" + urllib.parse.urlencode(dict(format="json", q=cat, viewbox=vb, bounded=1, limit=15, **{"accept-language": "en"}))) or []
        for x in j:
            if x.get("name"):
                out.append([x["name"], float(x["lat"]), float(x["lon"]), 1000 * (len(NOM_CATS) - i) + float(x.get("importance") or 0) * 1000, x.get("type")])
    return sorted(out, key=lambda r: -r[3])


def landmark_list(lat, lng, half):
    """Landmarks around the pin: notable OSM attractions across the view (Nominatim, importance-ranked)
    first, then the nearest geotagged Wikipedia articles (length-ranked). Cached on disk by position and scale."""
    key = f"{lat:.3f},{lng:.3f},{half:.3f}"
    c = LMC.get(key)
    if c and c.get("wiki") is not None:
        LM_SRC[0] = _lm_source(c); return c["items"]
    nom = c["nom"] if c else nominatim_near(lat, lng, half)
    wk = wiki_near(lat, lng)
    seen, items = set(), []
    for it in nom + (wk or []):
        k = norm(it[0]) or it[0].lower()
        if k in seen: continue
        seen.add(k); items.append(it)
    LMC[key] = {"nom": nom, "wiki": wk, "items": items}; _save_cache()
    LM_SRC[0] = _lm_source(LMC[key])
    return items


LM_SRC = ["none"]
def _lm_source(c):
    """Where a view's landmarks came from: "wikipedia" (with or without Nominatim), "nominatim", or "none"."""
    if c.get("wiki"): return "wikipedia"
    return "nominatim" if c.get("nom") else "none"


def ne_points(P, view, boxes, excl, half):
    out = []
    if half > 1.5: return out
    for g, p in sorted(q("airports", view), key=lambda r: r[1]["r"]):
        if not view.contains(g) or not p.get("n"): continue
        x, y = P(g.x, g.y); t = p["n"] if "airport" in p["n"].lower() else p["n"] + " Airport"
        if norm(t) in excl: continue
        w = tw(t, 12, 0.55) + 16
        if boxes.take((x - 7, y - 10, x + w, y + 7)):
            out.append(f'<path class="ap" d="M{x-6:.0f} {y:.0f}h12M{x:.0f} {y-6:.0f}v12"/><text class="lt" x="{x+10:.0f}" y="{y+4:.0f}">{html.escape(t)}</text>')
            excl.add(norm(t))
    return out


def landmarks(P, view, boxes, excl, wl, maxn=9):
    out, pos = [], []
    for t, la, lo, ln, ty in wl:
        if len(out) >= maxn: break
        s = re.sub(r"\s*\([^)]*\)$", "", t)
        s = re.sub(r",\s[^,]+$", "", s)
        k = norm(s) or s.lower()
        if k in excl or len(s) > 42 or not view.contains(Point(lo, la)): continue
        x, y = P(lo, la)
        if any(abs(x - a) < 6 and abs(y - b) < 6 for a, b in pos): continue
        w = tw(s, 12, 0.55) + 10
        for r, tx, anc in (((x - 5, y - 11, x + w, y + 6), x + 8, "start"), ((x - w, y - 11, x + 5, y + 6), x - 8, "end"),
                           ((x - w / 2, y - 26, x + w / 2, y - 6), x, "middle")):
            if boxes.take(r):
                ty_ = y + 4 if anc != "middle" else y - 10
                it = ty in ("river", "waterbody", "mountain", "glacier", "isle")
                out.append(f'<path class="lm" d="M{x:.0f} {y-4:.0f}l4 4-4 4-4-4z"/>'
                           f'<text class="lt" x="{tx:.0f}" y="{ty_:.0f}" text-anchor="{anc}"{" font-style=\"italic\"" if it else ""}>{html.escape(s)}</text>')
                pos.append((x, y)); excl.add(k)
                break
    return out


def inset(lat, lng, x, y, w, h, half_r, main_view, guess_hl):
    P2 = Proj(lat, lng, half_r, x + w / 2, y + h / 2, h / 2)
    v2 = P2.view(w / 2 + 4, h / 2 + 4)
    t = 2.4
    S = [f'<rect class="ps" x="{x}" y="{y}" width="{w}" height="{h}"/>']
    for g, _ in q("land", v2): S.append(f'<path class="ld" d="{d_of(g, P2, v2, tol=t)}"/>')
    for g, p in q("regions", v2):
        c = (p.get("c") or "").lower()
        cls = "ds" if c == "desert" else "mt" if c in ("range/mtn", "foothills", "plateau") else None
        if cls: S.append(f'<path class="{cls}" d="{d_of(g, P2, v2, tol=t*2)}"/>')
    for g, p in q("lakes", v2):
        if p["r"] <= 7: S.append(f'<path class="lk" d="{d_of(g, P2, v2, tol=t)}"/>')
    for g, p in q("rivers", v2):
        if p["r"] <= 7: S.append(f'<path class="rv" stroke-width="1.2" d="{d_of(g, P2, v2, False, tol=t)}"/>')
    for g, _ in q("adm0", v2): S.append(f'<path class="b0" d="{d_of(g, P2, v2, False, tol=t)}"/>')
    b = main_view.bounds
    (ax, ay), (bx, by) = P2(b[0], b[3]), P2(b[2], b[1])
    if abs(bx - ax) > 6:
        S.append(f'<rect class="gr" x="{min(ax,bx):.0f}" y="{min(ay,by):.0f}" width="{abs(bx-ax):.0f}" height="{abs(by-ay):.0f}"/>')
    cx, cy = P2(lng, lat)
    S.append(f'<circle class="gp" cx="{cx:.0f}" cy="{cy:.0f}" r="3"/>')
    S = [s for s in S if 'd=""' not in s]
    return (f'<rect class="lb" x="{x}" y="{y}" width="{w}" height="{h}" rx="10" filter="url(#s)"/><clipPath id="ic"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/></clipPath>'
            f'<g clip-path="url(#ic)">{"".join(S)}</g><rect class="go" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>')


REGION_TYPES = {"continent", "country", "state", "region", "province", "archipelago", "island"}
# Where reverse geocoding gives contested local names (e.g. a refugee camp near Bethlehem labelled
# "Gush Etzion Regional Council"): the subtitle comes from the job's parent or the nearest city instead.
CONTESTED = [(31.20, 32.60, 34.88, 35.60)]   # (south, north, west, east): the West Bank and Jerusalem


def contested(lat, lng):
    return any(s <= lat <= n and w <= lng <= e for s, n, w, e in CONTESTED)


def nearest_city(lat, lng, minpop=10000):
    k = max(math.cos(math.radians(lat)), 0.2)
    best = min((c for c in CITIES or [] if c[3] >= minpop), default=None,
               key=lambda c: (c[1] - lat) ** 2 + ((c[2] - lng) * k) ** 2)
    return best[0] if best else None


def contains_pin(rec, lat, lng):
    """Whether a Nominatim result's own polygon contains the pin (a point result never does)."""
    g = poly(rec)
    return g is not None and g.buffer(0.002).contains(Point(lng, lat))


def area_name(lat, lng):
    """Country or sea under the pin, from Natural Earth: the subtitle when reverse geocoding finds nothing."""
    pt = Point(lng, lat)
    for layer in ("countries", "marine"):
        for g, p in q(layer, pt.buffer(0.05)):
            if p.get("n") and g.distance(pt) < 0.05: return p["n"]
    return None


def region_half(rec, lat, lng):
    """A region's view half-height from its Nominatim bounding box, when it has no outline."""
    bb = (rec or {}).get("boundingbox")
    try:
        s_, n_, w_, e_ = (float(x) for x in bb)
    except (TypeError, ValueError):
        return 6.0
    if not (s_ - 1 <= lat <= n_ + 1 and w_ - 1 <= lng <= e_ + 1): return 6.0
    return min(max((n_ - s_) * 0.6, 1.0), 24)


def make(job, outdir, lite=0):
    name, lat, lng = job["name"], float(job["lat"]), float(job["lng"])
    guess = job.get("confidence") in ("inferred", "best-guess")
    pole = abs(lat) >= 85   # equirectangular is degenerate here: always the polar (orthographic) view
    if pole: lat = math.copysign(min(abs(lat), 89.5), lat)
    half_set = job.get("half", job.get("zoom"))
    half_set = min(max(float(half_set), 0.003), 30.0) if half_set not in (None, "") else None
    rec = nominatim(job.get("query"), job.get("osm"))
    if rec and (abs(float(rec.get("lat", lat)) - lat) > 1.5 or abs(float(rec.get("lon", lng)) - lng) > 1.5):
        g0 = poly(rec)
        if g0 is None or g0.distance(Point(lng, lat)) > 0.05: rec = None   # matched some other place of that name
    g = poly(rec)
    if g is not None and g.distance(Point(lng, lat)) > 0.02:
        g = None
    par = poly(nominatim(job.get("parent"))) if job.get("parent") else None
    if par is not None and par.distance(Point(lng, lat)) > 0.2: par = None
    rank = int((rec or {}).get("place_rank", 30))
    gb = g
    if g is not None and g.geom_type == "MultiPolygon":
        parts = sorted(g.geoms, key=lambda p: -p.area)
        gb = MultiPolygon([p for p in parts if p.area >= parts[0].area * 0.03])
    gh = (gb.bounds[3] - gb.bounds[1]) if g is not None else 0
    settlement = rank <= 16 or (g is not None and gh > 0.04 and (rec or {}).get("class") in ("boundary", "place"))
    px, py, pw, ph = 200, 90, 800, 360
    mx, my, hpx = px + pw / 2, py + (ph - 52) / 2, (ph - 52) / 2
    regional = ((g is not None and gh > 0.3) or (g is None and rank <= 12) or bool(job.get("region"))
                or (rec or {}).get("addresstype") in REGION_TYPES or pole)
    excl = {norm(name), norm(job.get("query") or ""), norm((job.get("query") or "").split(",")[0])} | {norm(x) for x in job.get("aliases") or []}
    wl = []
    LM_SRC[0] = "none"
    if half_set is not None:
        half = half_set
        if not regional: wl = landmark_list(lat, lng, round(half, 3))
    elif regional:
        half = min(max(gh * 0.75, 0.3), 24) if g is not None else region_half(rec, lat, lng)
        if pole: half = max(half, 8.0)
    else:
        half = min(max(gh * 0.85, 0.1 if settlement else 0.045), 0.3)
        for _ in range(5):   # zoom out until there is something to show around the pin
            wl = WIKI_PRE.get(f"{lat:.3f},{lng:.3f}")
            if wl is None:
                wl = wiki_near(lat, lng)
                if wl is None:   # Wikipedia throttled: no basis for zooming, keep the default extent
                    wl = []; break
                WIKI_PRE[f"{lat:.3f},{lng:.3f}"] = wl
            k = max(math.cos(math.radians(lat)), 0.2)
            nwl = sum(1 for t, la, lo, *_ in wl if abs(la - lat) < half and abs(lo - lng) * k < half * 2.4 and norm(t) not in excl)
            ntw = sum(1 for c in CITIES if abs(c[1] - lat) < half and abs(c[2] - lng) * k < half * 2.4 and c[3] >= 1000)
            if nwl + ntw >= 5 or half >= 2.0: break
            half = min(half * 2.5, 0.3 if nwl + ntw >= 2 else 2.0)
        # dense places (central London): the nearest articles cluster round the pin, so zoom in to spread them out
        spots = {(round(la, 3), round(lo, 3)) for t, la, lo, *_ in wl if norm(t) not in excl}
        ds = sorted(math.hypot(la - lat, (lo - lng) * k) for la, lo in spots)
        if len(ds) >= 40:
            r80 = ds[int(len(ds) * 0.8)]
            if r80 * 0.9 < half:
                half = max(r80 * 0.9, gh * 0.85, 0.03)
        wl = landmark_list(lat, lng, round(half, 3))
    P = Proj(lat, lng, half, mx, my, hpx)
    view = P.view(pw / 2 + 40, ph / 2 + 60)
    boxes = Boxes((px + 14, py + 18, px + pw - 14, py + ph - 58))
    cx, cy = P(lng, lat)
    boxes.add((cx - 15, cy - 15, cx + 15, cy + 15))
    gx, gy, gr = px + 58, py + 58, 40
    boxes.add((px, py, gx + gr + 12, gy + gr + 40))   # globe + scale bar
    ix, iy, iw, ih = px + pw - 196, py + 16, 180, 112
    show_inset = not regional
    if show_inset: boxes.add((ix - 10, iy - 10, ix + iw + 10, iy + ih + 10))
    lx, ly, lr = px + pw - 106, py + 210 if show_inset else py + 110, 72
    big = g is not None and gh * P.sc > 10
    use_lens = g is not None and not big and gh > 0.003
    if use_lens: boxes.add((lx - lr - 12, ly - lr - 12, lx + lr + 12, ly + lr + 26))
    A = []
    polar = (abs(lat) > 64 and half > 1.5) or pole
    if polar:   # equirectangular breaks down near the poles: draw the area on an orthographic globe instead
        R = hpx / math.radians(max(half, 2))
        f = ortho(lat, lng, mx, my, R)
        def od(geom):
            out = []
            for p in (list(geom.geoms) if hasattr(geom, "geoms") else [geom]):
                if p.geom_type != "Polygon": continue
                pts = [f(x, y) for x, y in p.exterior.coords]
                if not any(v for _, _, v in pts): continue
                ls = LineString([(a, b) for a, b, _ in pts]).simplify(TOL[0])
                if ls.length > 8: out.append(_ring(list(ls.coords), True))
            return "".join(out)
        S = [f'<circle class="ld" cx="{mx}" cy="{my}" r="{R:.0f}" style="fill:var(--sea)"/>']
        for gg in C50():
            if gg.is_empty: continue
            c = gg.centroid
            if abs(c.y - lat) < 60 or gg.intersects(Point(lng, lat).buffer(30)):
                d = od(gg.simplify(0.08))
                if d: S.append(f'<path class="ld" d="{d}"/>')
        for la in (-80, -70, -60, 60, 70, 80):
            pts = [f(lo, la) for lo in range(-180, 181, 6)]
            if any(v for *_, v in pts): S.append(f'<path class="b1" d="{_ring([(a, b) for a, b, v in pts], False)}"/>')
        if g is not None:
            d = od(g.simplify(0.08))
            if d: S.append(f'<path class="ol" d="{d}"/>')
        regs, rivers, big, use_lens = [], [], False, False
    else:
        S, regs, rivers = basemap(P, view, half, A, boxes, lite)
    if par is not None and (par.bounds[3] - par.bounds[1]) > half * 0.08:
        d = d_of(par, P, view)
        if d: S.append(f'<path class="pa" d="{d}"/>')
    if g is not None and not use_lens and not polar:
        d = d_of(g, P, view, tol=min(TOL[0], 0.8) if not big else None)
        if d: S.append(f'<path class="ol" d="{d}"/>')
    # labels, by priority
    Lb = []
    town = None
    if not settlement and not regional and job.get("pin_label", True) is not False and not contested(lat, lng):
        rv = reverse(round(lat, 4), round(lng, 4), 10) or {}
        a = rv.get("address", {})
        town = a.get("city") or a.get("town") or a.get("village")
        # A neighbouring place labelled at the pin (Alpe d'Huez showed "Auris"): only label a place whose
        # own boundary holds the pin, and never one the job's parent contradicts.
        if town and not contains_pin(rv, lat, lng): town = None
        if town and job.get("parent") and norm(town) not in norm(job["parent"]): town = None
        if town and norm(town) not in excl:
            w = tw(town, 16, 0.58)
            if boxes.take((cx + 18, cy - 34, cx + 26 + w, cy - 10)):
                Lb.append(f'<text class="pl" x="{cx+20:.0f}" y="{cy-15:.0f}">{html.escape(town)}</text>')
            excl.add(norm(town))
        else:
            town = None
    if polar: Lb = []
    else: Lb += towns(P, view, half, boxes, excl, settlement, maxn=6 if wl else 10)
    if not polar:
        Lb += ne_points(P, view, boxes, excl, half)
        if wl: Lb += landmarks(P, view, boxes, excl, wl, maxn=7 if lite else 11)
        Lb += labels_rivers(P, view, rivers, boxes, A)
        Lb += labels_regions(P, view, regs, boxes, half)
        Lb += labels_points(P, view, boxes, half)
    lens = ""
    if use_lens:
        lhalf = max(gh * 1.4, 0.004)
        if par is not None and (par.bounds[3] - par.bounds[1]) < lhalf * 6:
            lhalf = max(lhalf, (par.bounds[3] - par.bounds[1]) * 0.6)
        Q = Proj(lat, lng, lhalf, lx, ly, lr * 0.85)
        lv = Q.view(lr + 10, lr + 10)
        inner = []
        for gg, _ in q("land", lv): inner.append(f'<path class="ld" d="{d_of(gg, Q, lv)}"/>')
        for gg, _ in q("lakes", lv): inner.append(f'<path class="lk" d="{d_of(gg, Q, lv)}"/>')
        for gg, _ in q("rivers", lv): inner.append(f'<path class="rv" stroke-width="2.4" d="{d_of(gg, Q, lv, False)}"/>')
        if par is not None:
            inner.append(f'<path class="pa" d="{d_of(par, Q, lv)}"/>')
        inner.append(f'<path class="ol" d="{d_of(g, Q, lv)}"/>')
        km = 2 * lhalf * 111
        kml = f"{km:.1f} km" if km < 10 else f"{km:.0f} km"
        inner = "".join(i for i in inner if 'd=""' not in i)
        lens = (f'<line class="ll" x1="{cx:.0f}" y1="{cy:.0f}" x2="{lx - lr*0.7:.0f}" y2="{ly + lr*0.7:.0f}"/>'
                f'<clipPath id="l"><circle cx="{lx}" cy="{ly}" r="{lr}"/></clipPath>'
                f'<circle class="lb" cx="{lx}" cy="{ly}" r="{lr}" filter="url(#s)"/>'
                f'<g clip-path="url(#l)">{inner}<rect x="{lx-lr}" y="{ly-lr}" width="{2*lr}" height="{2*lr}" fill="url(#m)"/>'
                f'<circle class="pin" cx="{lx}" cy="{ly}" r="5" stroke-width="2"/></g>'
                f'<circle class="lr" cx="{lx}" cy="{ly}" r="{lr}"/>'
                f'<text class="st" x="{lx}" y="{ly+lr+16}" text-anchor="middle">{kml} across</text>')
    hl = next((cg for cg, _ in q("countries", Point(lng, lat).buffer(0.01)) if cg.distance(Point(lng, lat)) < 0.05), None)
    G = globe(lat, lng, gx, gy, gr, hl, view)
    SB = scalebar(P, px + 20, gy + gr + 30)
    INS = inset(lat, lng, ix, iy, iw, ih, max(half * 7, 1.0), view, guess) if show_inset else ""
    # subtitle
    sub = job.get("subtitle")
    if not sub and contested(lat, lng):
        near = nearest_city(lat, lng)
        sub = (job.get("parent") or "").split(",")[0].strip() or (f"near {near}" if near and norm(near) != norm(name) else "")
    if not sub:
        ra = (reverse(round(lat, 4), round(lng, 4), 10) or {}).get("address", {})
        loc = ra.get("city") or ra.get("town") or ra.get("village") or ra.get("municipality") or ra.get("county")
        st, ctry = ra.get("state"), ra.get("country")
        parts = [st] if settlement else [loc if loc and norm(loc) != norm(name) else st]
        if ctry in ("United States", "United Kingdom") and not settlement and parts[0] != st: parts.append(st)
        elif ctry: parts.append(ctry)
        parts = [p for p in dict.fromkeys(parts) if p and norm(p) != norm(name)]
        if not parts and ctry and norm(ctry) != norm(name): parts = [ra.get("continent") or ctry]
        sub = " · ".join(parts[:2])
    if not sub:   # nothing from reverse geocoding (a pole, open sea)
        an = area_name(lat, lng)
        sub = an if an and norm(an) != norm(name) else ""
    c1, c2, label = ICON.get(job.get("icon"), ICON["book"])
    conf = job.get("confidence")
    tag = "From reading" + (" · best guess" if conf == "best-guess" else " · inferred" if conf == "inferred" else "")
    e = html.escape
    avail = pw - 80 - 92 - 24
    disp = name
    fs = 30
    while tw(disp, fs, 0.58) > avail and fs > 19: fs -= 1
    while tw(disp, fs, 0.58) > avail and len(disp) > 8: disp = disp[:-2].rstrip() + "…"
    sfs = 18
    while tw(sub, sfs, 0.52) > avail and sfs > 13: sfs -= 1
    lw = 30 + len(label) * 10
    coord = f"{lat:.4f}, {lng:.4f}"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
           f'<title>{e(name)}</title>{css(DARK, LIGHT, guess)}<g id="light"/><g id="dark"/><g id="r">'
           f'<defs>{"".join(A)}'
           f'<radialGradient id="a" cx="78%" cy="18%" r="70%"><stop class="s1" offset="0" stop-opacity=".9"/><stop class="s0" offset="1" stop-opacity="0"/></radialGradient>'
           f'<radialGradient id="b" cx="15%" cy="95%" r="50%"><stop class="s2" offset="0" stop-opacity=".55"/><stop class="s0" offset="1" stop-opacity="0"/></radialGradient>'
           f'<pattern id="g" width="32" height="32" patternUnits="userSpaceOnUse"><path class="gg" d="M32 0H0V32"/></pattern>'
           f'<pattern id="m" width="40" height="40" patternUnits="userSpaceOnUse"><path class="mg" d="M40 0H0V40"/></pattern>'
           f'<pattern id="pd" width="9" height="9" patternUnits="userSpaceOnUse"><circle class="pdd" cx="2" cy="2" r=".9"/><circle class="pdd" cx="6.5" cy="6.5" r=".9"/></pattern>'
           f'<pattern id="pm" width="18" height="14" patternUnits="userSpaceOnUse"><path class="pmm" d="M2 9l4-5 4 5"/><path class="pmm" d="M11 13l3-4 3 4"/></pattern>'
           f'<pattern id="pw" width="14" height="8" patternUnits="userSpaceOnUse"><path class="pww" d="M1 5h6M8 2h5"/></pattern>'
           f'<clipPath id="c"><rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="16"/></clipPath>'
           f'<linearGradient id="i" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>'
           f'<radialGradient id="p"><stop class="pc" offset="0" stop-opacity=".55"/><stop class="pc" offset="1" stop-opacity="0"/></radialGradient>'
           f'<filter id="s" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow class="sh" dx="0" dy="10" stdDeviation="14" flood-color="#000"/></filter>'
           f'</defs>'
           f'<rect class="pb" width="{W}" height="{H}"/><rect width="{W}" height="{H}" fill="url(#a)"/><rect width="{W}" height="{H}" fill="url(#b)"/><rect class="pg" width="{W}" height="{H}"/>'
           f'<rect class="pn" x="{px}" y="{py}" width="{pw}" height="{ph}" rx="16" filter="url(#s)"/>'
           f'<g clip-path="url(#c)" font-family="{FONT}"><rect class="ps" x="{px}" y="{py}" width="{pw}" height="{ph}"/>{"".join(S)}'
           f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" fill="url(#m)"/>{"".join(Lb)}'
           f'<circle class="ph" cx="{cx:.0f}" cy="{cy:.0f}" r="40"/><circle class="pin" cx="{cx:.0f}" cy="{cy:.0f}" r="9"/>{lens}{INS}{G}{SB}</g>'
           f'<rect class="fr" x="{px}" y="{py}" width="{pw}" height="{ph}" rx="16"/>'
           f'<rect class="cd" x="{px+40}" y="{py+ph-52}" width="{pw-80}" height="92" rx="14" filter="url(#s)"/>'
           f'<rect x="{px+64}" y="{py+ph-30}" width="48" height="48" rx="11" fill="url(#i)"/>'
           f'<g font-family="{FONT}"><text class="t1" x="{px+132}" y="{py+ph-4}" font-size="{fs}" font-weight="600">{e(disp)}</text>'
           f'<text class="t3" x="{px+132}" y="{py+ph+24}" font-size="{sfs}">{e(sub)}</text>'
           f'<rect class="ch" x="{px}" y="{H-78}" width="{lw}" height="34" rx="8"/><text class="t1" x="{px+15}" y="{H-55}" font-size="17">{label}</text>'
           f'<text class="t3" x="{px+lw+15}" y="{H-55}" font-size="17">{e(tag)}</text></g>'
           f'<text class="co" x="{px+pw}" y="{H-55}" text-anchor="end" font-family="{MONO}" font-size="15">{coord}</text>'
           f'</g></svg>')
    return svg, dict(name=name, outline=g is not None, lens=bool(lens), parent=par is not None, half=round(half, 3),
                     settlement=settlement, regional=regional, polar=polar, subtitle=sub,
                     pin_label=town if (town and not settlement) else None, landmarks=LM_SRC[0])


def split_theme(svg, theme):
    """Turn the two-theme SVG into a single-theme one (for --split)."""
    if theme == "light":
        m = re.search(r"#light:target~#r\{([^}]*)\}", svg)
        svg = re.sub(r"#r\{[^}]*\}", "#r{" + m.group(1) + "}", svg, count=1)
    svg = re.sub(r"#light:target~#r\{[^}]*\}#dark:target~#r\{[^}]*\}", "", svg)
    return svg.replace('<g id="light"/><g id="dark"/>', "")


TARGET_BYTES = 32000   # a target, not a cap: the coarsest retry is kept even when it is larger


def run(jobs, out):
    """Render each job to OUT/<index>.svg; one JSON line per job on stdout, in order."""
    os.makedirs(out, exist_ok=True)
    load()
    for i, j in enumerate(jobs):
        try:
            t0 = time.time()
            for t, lite in ((1.2, 0), (2.0, 0), (2.0, 1), (3.2, 2), (5.0, 3), (8.0, 3)):
                TOL[0] = t
                svg, r = make(j, out, lite)
                if len(svg.encode()) <= TARGET_BYTES: break
            data = svg.encode("utf-8")
            _write_atomic(os.path.join(out, f"{i}.svg"), data)
            r.update(index=i, bytes=len(data), over_target=len(data) > TARGET_BYTES, tol=t, lite=lite,
                     secs=round(time.time() - t0, 1))
            print(json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as ex:
            import traceback; traceback.print_exc()
            print(json.dumps(dict(index=i, name=j.get("name"), error=f"{type(ex).__name__}: {ex}"), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--setup"]:
        print(json.dumps({"setup": "ok", "files": setup()}), flush=True); sys.exit(0)
    jobs_path, out_dir = sys.argv[1], sys.argv[2]
    with open(jobs_path, encoding="utf-8") as f:
        run(json.load(f), out_dir)
