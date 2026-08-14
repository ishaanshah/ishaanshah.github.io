#!/usr/bin/env python3
"""Sync the Trips gallery from Immich.

Reads the curated manifest `_data/trips.yml`, then for each outing:
  * resolves its Immich album (by name or UUID),
  * pulls the assets + EXIF (GPS, capture time, dimensions, description),
  * downloads two WebP derivatives (grid + lightbox) into assets/trips/<region>/<outing>/,
  * computes distance / ascent / moving-time from the outing's GPX file,
and writes the materialised `_data/regions.json` + `_regions/<region>.md` stubs
that Jekyll renders. The live site then serves only the static WebP — it never
talks to Immich.

Config via environment:
  IMMICH_URL   e.g. https://photos.example.com   (no trailing /api)
  IMMICH_KEY   an Immich API key (Account Settings -> API Keys)

Usage:
  python3 tools/sync_immich.py            # full sync
  python3 tools/sync_immich.py --albums   # list albums and exit (find names/UUIDs)
  python3 tools/sync_immich.py --dry-run  # resolve + report, download nothing

Dependencies:  requests, pyyaml, pillow   (pip install requests pyyaml pillow)

NOTE: Immich's REST paths have shifted across versions. This targets the
/api/albums + /api/assets/{id}/thumbnail shape (Immich >= ~1.94). If your
instance 404s, check its /api/api-docs and adjust ENDPOINTS below.
"""
import argparse, io, json, math, os, sys, xml.etree.ElementTree as ET
from datetime import date

try:
    import requests, yaml
except ImportError:
    sys.exit("Missing deps: pip install requests pyyaml pillow")
try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_W, FULL_W = 800, 1800          # derivative widths (px)

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH_KEY = os.environ.get("IMMICH_KEY", "")
S = requests.Session()
S.headers.update({"x-api-key": IMMICH_KEY, "Accept": "application/json"})

ENDPOINTS = dict(
    albums="{base}/api/albums",
    album="{base}/api/albums/{id}",
    thumbnail="{base}/api/assets/{id}/thumbnail?size=preview",   # ~1440px preview
)

# ---------------- Immich helpers ----------------
def api(url):
    r = S.get(url, timeout=60)
    r.raise_for_status()
    return r.json()

def list_albums():
    return api(ENDPOINTS["albums"].format(base=IMMICH_URL))

def resolve_album(ident, albums):
    for a in albums:
        if a.get("id") == ident or a.get("albumName") == ident:
            return a["id"]
    return None

def album_assets(album_id):
    full = api(ENDPOINTS["album"].format(base=IMMICH_URL, id=album_id))
    assets = [a for a in full.get("assets", []) if a.get("type") == "IMAGE"]
    # chronological order → matches how a trip unfolds
    assets.sort(key=lambda a: (a.get("exifInfo") or {}).get("dateTimeOriginal")
                or a.get("localDateTime") or "")
    return assets

def oriented_dims(ex):
    w, h = ex.get("exifImageWidth") or 1600, ex.get("exifImageHeight") or 1200
    if str(ex.get("orientation") or "") in ("5", "6", "7", "8"):
        w, h = h, w
    return int(w), int(h)

def download_derivatives(asset_id, out_dir, stem):
    """Fetch the preview and write grid + full WebP. Returns (grid, full, w, h)."""
    os.makedirs(out_dir, exist_ok=True)
    url = ENDPOINTS["thumbnail"].format(base=IMMICH_URL, id=asset_id)
    raw = S.get(url, timeout=120); raw.raise_for_status()
    rel = os.path.relpath(out_dir, ROOT).replace(os.sep, "/")
    if HAVE_PIL:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw.content))).convert("RGB")
        def save(width, suffix):
            w, h = im.size
            scale = min(1.0, width / float(w))
            r = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            p = os.path.join(out_dir, stem + suffix + ".webp")
            r.save(p, "WEBP", quality=82, method=6)
            return "/" + rel + "/" + os.path.basename(p), r.size
        grid, _ = save(GRID_W, "-thumb")
        full, (fw, fh) = save(FULL_W, "")
        return grid, full, fw, fh
    # no Pillow: dump the preview as-is for both sizes
    p = os.path.join(out_dir, stem + ".jpg")
    with open(p, "wb") as f:
        f.write(raw.content)
    src = "/" + rel + "/" + os.path.basename(p)
    return src, src, None, None

# ---------------- GPX stats ----------------
def haversine(a, b):
    R, tr = 6371000.0, math.pi / 180
    dlat = (b[0] - a[0]) * tr; dlng = (b[1] - a[1]) * tr
    h = (math.sin(dlat / 2) ** 2 +
         math.cos(a[0] * tr) * math.cos(b[0] * tr) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

def gpx_stats(path, kmh):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    pts = []
    for tp in root.iterfind(".//g:trkpt", ns) or []:
        ele = tp.find("g:ele", ns)
        pts.append((float(tp.get("lat")), float(tp.get("lon")),
                    float(ele.text) if ele is not None else 0.0))
    if len(pts) < 2:
        return None
    dist = sum(haversine(pts[i - 1], pts[i]) for i in range(1, len(pts)))
    ascent = sum(max(0.0, pts[i][2] - pts[i - 1][2]) for i in range(1, len(pts)))
    hours = (dist / 1000.0) / kmh
    h, m = int(hours), int(round((hours - int(hours)) * 60))
    if m == 60: h, m = h + 1, 0
    return ("%.1f km" % (dist / 1000.0), "%d m" % (int(round(ascent / 10.0)) * 10),
            "%dh %02dm" % (h, m), dist / 1000.0)

SPEED = {"hike": 3.6, "bike": 15.0, "run": 9.5}
# Okabe–Ito colourblind-safe palette — highly distinguishable over the topo basemap
ROUTE_COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]

# ---------------- dates derived from photo capture times ----------------
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def asset_date(asset):
    """Capture date of an Immich asset, or None."""
    ex = asset.get("exifInfo") or {}
    s = ex.get("dateTimeOriginal") or asset.get("localDateTime") or ""
    try:
        return date.fromisoformat(s[:10])          # YYYY-MM-DD prefix is version-safe
    except ValueError:
        return None

def widen(span, d):
    """Fold a date into a (lo, hi) span."""
    if d is None:
        return span
    lo, hi = span
    return (d if lo is None or d < lo else lo,
            d if hi is None or d > hi else hi)

def fmt_span(span):
    lo, hi = span
    if lo is None:
        return ""
    if (lo.year, lo.month) == (hi.year, hi.month):
        return "%s %d" % (MONTHS[lo.month], lo.year)
    if lo.year == hi.year:
        return "%s–%s %d" % (MONTHS[lo.month], MONTHS[hi.month], hi.year)
    return "%s %d – %s %d" % (MONTHS[lo.month], lo.year, MONTHS[hi.month], hi.year)

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--albums", action="store_true", help="list Immich albums and exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve + report, download nothing")
    args = ap.parse_args()

    if not IMMICH_URL or not IMMICH_KEY:
        sys.exit("Set IMMICH_URL and IMMICH_KEY environment variables.")

    albums = list_albums()
    if args.albums:
        for a in sorted(albums, key=lambda x: x.get("albumName", "")):
            print("%-40s %s" % (a.get("albumName"), a.get("id")))
        return
    if not HAVE_PIL and not args.dry_run:
        print("WARNING: Pillow not installed — saving full-size JPEG previews (no WebP resize).")

    manifest = yaml.safe_load(open(os.path.join(ROOT, "_data", "trips.yml")))
    regions_out = []
    for r in manifest["regions"]:
        outings_out, total_km = [], 0.0
        reg_span = (None, None)
        for oi, o in enumerate(r["outings"]):
            color = ROUTE_COLORS[oi % len(ROUTE_COLORS)]
            album_id = resolve_album(o["immich_album"], albums)
            if not album_id:
                print("  ! album not found: %r (outing %s) — skipping" % (o["immich_album"], o["id"]))
                continue
            assets = album_assets(album_id)
            print("  %s/%s: %d photos" % (r["id"], o["id"], len(assets)))

            entry = dict(id=o["id"], name=o["name"], activity=o["activity"], color=color)
            if o.get("gpx"):
                st = gpx_stats(os.path.join(ROOT, o["gpx"]), SPEED.get(o["activity"], 4.0))
                if st:
                    entry.update(gpx="/" + o["gpx"].lstrip("/"),
                                 distance=st[0], ascent=st[1], duration=st[2])
                    total_km += st[3]

            out_dir = os.path.join(ROOT, "assets", "trips", r["id"], o["id"])
            photos = []
            o_span = (None, None)
            for i, a in enumerate(assets):
                o_span = widen(o_span, asset_date(a))
                ex = a.get("exifInfo") or {}
                w, h = oriented_dims(ex)
                p = dict(caption=ex.get("description") or "", width=w, height=h)
                if ex.get("latitude") is not None and ex.get("longitude") is not None:
                    p["lat"] = round(ex["latitude"], 6)
                    p["lng"] = round(ex["longitude"], 6)
                if args.dry_run:
                    p["grid"] = p["full"] = "(dry-run)"
                else:
                    grid, full, gw, gh = download_derivatives(a["id"], out_dir, "%02d" % i)
                    p["grid"], p["full"] = grid, full
                    if gw: p["width"], p["height"] = gw, gh
                photos.append(p)
            # date: manifest value wins, else derived from photo capture times
            entry["date"] = o.get("date") or fmt_span(o_span)
            entry["photos"] = photos
            reg_span = widen(widen(reg_span, o_span[0]), o_span[1])
            outings_out.append(entry)

        if not outings_out:
            continue
        # cover fallback = first photo's thumbnail (JS randomises it per load)
        cover = outings_out[0]["photos"][0].get("grid") if outings_out[0]["photos"] else ""
        reg = dict(id=r["id"], name=r["name"], area=r["area"],
                   dates=r.get("dates") or fmt_span(reg_span),
                   kind=r["kind"], cover=cover, outings=outings_out)
        if r["kind"] == "outdoor":
            reg["total_distance"] = "%.1f km" % total_km
        regions_out.append(reg)

        stub = os.path.join(ROOT, "_regions", r["id"] + ".md")
        os.makedirs(os.path.dirname(stub), exist_ok=True)
        with open(stub, "w") as f:
            f.write("---\nlayout: region\nregion_id: %s\ntitle: %s\n---\n" % (r["id"], r["name"]))

    if not args.dry_run:
        with open(os.path.join(ROOT, "_data", "regions.json"), "w") as f:
            json.dump(regions_out, f, indent=2, ensure_ascii=False)
    print("Done: %d regions." % len(regions_out))

if __name__ == "__main__":
    main()
