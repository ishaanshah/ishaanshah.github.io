#!/usr/bin/env python3
"""Sync the Trips gallery from Immich.

Reads the curated manifest `_data/trips.yml`, then for each outing:
  * resolves its Immich album (by name or UUID),
  * pulls the assets + EXIF (GPS, capture time, dimensions, description),
  * downloads two WebP derivatives (grid + lightbox) into assets/trips/<collection>/<outing>/,
  * computes distance / moving-time from the outing's GPX file, taking ascent
    and descent from Garmin's own figures when tools/fetch_garmin.py has cached
    them (GPX `<ele>` deltas are noisy and overstate climbing),
and writes the materialised `_data/collections.json` + `_pics/<collection>.md` stubs
that Jekyll renders. The live site then serves only the static WebP — it never
talks to Immich.

Config via environment:
  IMMICH_URL   e.g. https://photos.example.com   (no trailing /api)
  IMMICH_KEY   an Immich API key (Account Settings -> API Keys)

Usage:
  python3 tools/sync_immich.py            # full sync
  python3 tools/sync_immich.py --albums   # list albums and exit (find names/UUIDs)
  python3 tools/sync_immich.py --dry-run  # resolve + report, download nothing
  python3 tools/sync_immich.py --force    # re-download every photo, ignoring the cache

Photos already downloaded are reused: `_data/immich_cache.json` remembers which
Immich asset produced which file, so a re-run only fetches photos that are new or
changed (and renames the rest in place when an album's order shifts). Delete that
file, or pass --force, to rebuild every derivative from scratch.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import focus as focus_mod            # focal points for the cover/thumbnail crops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_W, FULL_W = 800, 1800          # derivative widths (px)
QUALITY = 82                        # WebP quality of both derivatives
GARMIN_STATS = os.path.join(ROOT, "_data", "garmin_stats.json")
IMMICH_CACHE = os.path.join(ROOT, "_data", "immich_cache.json")
STAGE_DIR = ".sync-stage"           # scratch dir used while photos are reshuffled

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
            r.save(p, "WEBP", quality=QUALITY, method=6)
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

# ---------------- derivative cache ----------------
# Re-encoding every photo on every run is the slow part of a sync, and almost all
# of it is wasted: the pixels rarely change. `_data/immich_cache.json` records
# which asset produced which file on disk, so an unchanged photo is skipped.
# `--force` ignores the cache and rebuilds everything.
def derivative_params():
    """What the files on disk were produced with — change any of it and they're stale."""
    return dict(version=1, grid_w=GRID_W, full_w=FULL_W, quality=QUALITY, pil=HAVE_PIL)

def load_cache(force):
    """{asset id: entry} from the last run, or {} if forced/absent/stale."""
    if force:
        return {}
    try:
        with open(IMMICH_CACHE) as f:
            data = json.load(f)
    except (IOError, ValueError):
        return {}
    if data.get("params") != derivative_params():
        print("Derivative settings changed — rebuilding every photo.")
        return {}
    return data.get("assets") or {}

def save_cache(fresh, previous):
    """Write the entries this run produced, keeping still-valid older ones.

    An asset that wasn't synced this run (its album failed to resolve, say) keeps
    its entry so the next run can still reuse its files — but only if no asset
    from this run now owns those files, otherwise a photo re-added to an album
    later would claim derivatives that no longer show it.
    """
    owned = set()
    for e in fresh.values():
        owned.update(x for x in (e.get("grid"), e.get("full")) if x)
    merged = dict(fresh)
    for aid, e in previous.items():
        if aid not in merged and not ({e.get("grid"), e.get("full")} & owned):
            merged[aid] = e
    with open(IMMICH_CACHE, "w") as f:
        json.dump(dict(params=derivative_params(), assets=merged), f,
                  indent=2, sort_keys=True)

def asset_sig(a):
    """Identity of the pixels we derived from — changes if the photo is replaced or edited."""
    ex = a.get("exifInfo") or {}
    return "|".join(str(x or "") for x in (a.get("checksum"), a.get("updatedAt"),
                                           a.get("fileModifiedAt"), ex.get("orientation")))

def site_path(path):
    """Absolute path -> the site-absolute URL stored in collections.json."""
    return "/" + os.path.relpath(path, ROOT).replace(os.sep, "/")

def abs_path(url):
    return os.path.join(ROOT, url.lstrip("/").replace("/", os.sep))

def target_paths(out_dir, stem):
    """(grid, full) files this photo should end up in — the same file without Pillow."""
    if HAVE_PIL:
        return (os.path.join(out_dir, stem + "-thumb.webp"),
                os.path.join(out_dir, stem + ".webp"))
    p = os.path.join(out_dir, stem + ".jpg")
    return (p, p)

def uniq(paths):
    seen, out = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p); out.append(p)
    return out

def stage_moves(assets, cache, out_dir):
    """Park cached derivatives whose filename must change in out_dir/.sync-stage.

    Files are named by album position, so inserting one photo shifts every stem
    after it. Moving all of those aside *before* anything is written means a
    reshuffle can never overwrite a file another photo is still waiting to reuse.
    Returns {asset id: [staged paths]}.
    """
    staged, stage = {}, os.path.join(out_dir, STAGE_DIR)
    for i, a in enumerate(assets):
        e = cache.get(a["id"])
        if not e or e.get("sig") != asset_sig(a):
            continue
        srcs = uniq([abs_path(p) for p in (e.get("grid"), e.get("full"))])
        tgts = uniq(list(target_paths(out_dir, "%02d" % i)))
        if srcs == tgts or len(srcs) != len(tgts):
            continue
        if not all(os.path.exists(p) for p in srcs):
            continue
        if any(os.path.dirname(p) != os.path.normpath(out_dir) for p in srcs):
            continue                          # lives under another outing — leave it alone
        os.makedirs(stage, exist_ok=True)
        moved = []
        for src in srcs:
            dest = os.path.join(stage, os.path.basename(src))
            os.replace(src, dest)
            moved.append(dest)
        staged[a["id"]] = moved
    return staged

def reuse_derivatives(a, out_dir, stem, cache, staged):
    """(grid, full, w, h) for an unchanged photo, or None if it must be downloaded."""
    e = cache.get(a["id"])
    if not e or e.get("sig") != asset_sig(a):
        return None
    tgts = uniq(list(target_paths(out_dir, stem)))
    parked = staged.get(a["id"])
    if parked:
        os.makedirs(out_dir, exist_ok=True)
        for src, dest in zip(parked, tgts):
            os.replace(src, dest)
    elif not all(os.path.exists(p) for p in tgts):
        return None                           # cached, but the file is gone from the tree
    grid, full = target_paths(out_dir, stem)
    return site_path(grid), site_path(full), e.get("width"), e.get("height")

def clear_stage(out_dir):
    stage = os.path.join(out_dir, STAGE_DIR)
    if not os.path.isdir(stage):
        return
    left = os.listdir(stage)
    if left:
        print("    ! %d staged file(s) left in %s — they will be rebuilt next run"
              % (len(left), os.path.relpath(stage, ROOT)))
        return
    os.rmdir(stage)

# ---------------- GPX stats ----------------
def haversine(a, b):
    R, tr = 6371000.0, math.pi / 180
    dlat = (b[0] - a[0]) * tr; dlng = (b[1] - a[1]) * tr
    h = (math.sin(dlat / 2) ** 2 +
         math.cos(a[0] * tr) * math.cos(b[0] * tr) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

def gpx_stats(path, kmh):
    """Distance / ascent / descent / walking time from a GPX track.

    Ascent and descent here are the sum of raw `<ele>` deltas, which is only a
    fallback: consumer-GPS elevation jitters by a few metres per point, and
    summing that jitter over thousands of points inflates the climb well beyond
    what was actually walked. Garmin's own figure is preferred when available.
    """
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
    descent = sum(max(0.0, pts[i - 1][2] - pts[i][2]) for i in range(1, len(pts)))
    hours = (dist / 1000.0) / kmh
    h, m = int(hours), int(round((hours - int(hours)) * 60))
    if m == 60: h, m = h + 1, 0
    return dict(distance="%.1f km" % (dist / 1000.0), ascent_m=ascent, descent_m=descent,
                duration="%dh %02dm" % (h, m), km=dist / 1000.0)


def fmt_m(v):
    """Metres, rounded to the nearest 10 — the precision these numbers deserve."""
    return "%d m" % (int(round(v / 10.0)) * 10)


# ---------------- Garmin elevation ----------------
def load_garmin_stats():
    """Activity summaries cached by tools/fetch_garmin.py, keyed by activity id."""
    try:
        with open(GARMIN_STATS) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def garmin_elevation(outing, stats):
    """(ascent_m, descent_m) from Garmin for this outing, or None.

    An outing links either one activity (`garmin_activity:`) or, when its GPX is
    a hand-merged track, several (`garmin_activities:`) whose climbs are summed.
    Returns None unless every linked activity has a cached figure, so we never
    report a partial total as if it were the whole outing.
    """
    ids = [outing["garmin_activity"]] if outing.get("garmin_activity") else []
    ids += list(outing.get("garmin_activities") or [])
    if not ids:
        return None
    entries = [stats.get(str(i)) for i in ids]
    if any(e is None or e.get("ascent_m") is None or e.get("descent_m") is None
           for e in entries):
        return None
    return (sum(e["ascent_m"] for e in entries),
            sum(e["descent_m"] for e in entries))

SPEED = {"hike": 3.6, "bike": 15.0, "run": 9.5}
DEFAULT_SPEED = 4.0        # `activity:` is optional; unknown/absent walks at this pace
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

# ---------------- focal points ----------------
def focal_point(grid, asset_id, store, reseed=False):
    """The CSS `object-position` for one photo, detecting it if it's new.

    Hand-set entries (`src: manual`, written by `tools/focus.py --edit`) always
    win; auto ones are only recomputed on --reseed-focus."""
    entry = store["assets"].get(asset_id)
    if entry and (entry.get("src") == "manual" or not reseed):
        return focus_mod.as_css(entry)
    path = os.path.join(ROOT, grid.lstrip("/"))
    if not os.path.exists(path):
        return "50% 50%"
    faces = focus_mod.fetch_faces(S, IMMICH_URL, asset_id)
    fx, fy = focus_mod.compute_focus(path, faces)
    entry = dict(x=fx, y=fy, src="auto")
    store["assets"][asset_id] = entry
    return focus_mod.as_css(entry)

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--albums", action="store_true", help="list Immich albums and exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve + report, download nothing")
    ap.add_argument("--force", "--no-cache", dest="force", action="store_true",
                    help="ignore the derivative cache and re-download every photo")
    ap.add_argument("--reseed-focus", action="store_true",
                    help="recompute every auto focal point (hand-set ones are kept)")
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
    garmin_stats = load_garmin_stats()
    if not garmin_stats:
        print("No %s — elevation will come from GPX. Run tools/fetch_garmin.py for "
              "Garmin's figures." % os.path.relpath(GARMIN_STATS, ROOT))
    cache = load_cache(args.force)
    focus_store = focus_mod.load_store()
    fresh, reused, fetched = {}, 0, 0
    collections_out = []
    for c in manifest["collections"]:
        outings_out, total_km = [], 0.0
        col_span = (None, None)
        for oi, o in enumerate(c["outings"]):
            color = ROUTE_COLORS[oi % len(ROUTE_COLORS)]
            album_id = resolve_album(o["immich_album"], albums)
            if not album_id:
                print("  ! album not found: %r (outing %s) — skipping" % (o["immich_album"], o["id"]))
                continue
            assets = album_assets(album_id)
            print("  %s/%s: %d photos" % (c["id"], o["id"], len(assets)))

            entry = dict(id=o["id"], name=o["name"], color=color)
            if o.get("activity"):                 # optional — only pace estimation uses it
                entry["activity"] = o["activity"]
            if o.get("gpx"):
                st = gpx_stats(os.path.join(ROOT, o["gpx"]), SPEED.get(o.get("activity"), DEFAULT_SPEED))
                if st:
                    # Garmin's barometric ascent/descent beats summing GPX <ele>
                    gm = garmin_elevation(o, garmin_stats)
                    if gm is None and (o.get("garmin_activity") or o.get("garmin_activities")):
                        print("    (no cached Garmin stats for %s — using GPX elevation; "
                              "run tools/fetch_garmin.py)" % o["id"])
                    ascent_m, descent_m = gm or (st["ascent_m"], st["descent_m"])
                    entry.update(gpx="/" + o["gpx"].lstrip("/"),
                                 distance=st["distance"], ascent=fmt_m(ascent_m),
                                 descent=fmt_m(descent_m), duration=st["duration"],
                                 elevation_source="garmin" if gm else "gpx")
                    total_km += st["km"]

            out_dir = os.path.normpath(os.path.join(ROOT, "assets", "trips", c["id"], o["id"]))
            # one pass up front, before anything is written: see stage_moves()
            staged = {} if args.dry_run else stage_moves(assets, cache, out_dir)
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
                    got = reuse_derivatives(a, out_dir, "%02d" % i, cache, staged)
                    if got:
                        reused += 1
                    else:
                        got = download_derivatives(a["id"], out_dir, "%02d" % i)
                        fetched += 1
                    grid, full, gw, gh = got
                    p["grid"], p["full"] = grid, full
                    if gw: p["width"], p["height"] = gw, gh
                    fresh[a["id"]] = dict(sig=asset_sig(a), grid=grid, full=full,
                                          width=gw, height=gh)
                    # focal point for the cover / map-thumbnail crops: detected
                    # once and remembered, so re-syncs are cheap and anything
                    # corrected in `tools/focus.py --edit` is never clobbered
                    p["focus"] = focal_point(p["grid"], a["id"], focus_store,
                                             reseed=args.reseed_focus)
                photos.append(p)
            if not args.dry_run:
                clear_stage(out_dir)
            # date: manifest value wins, else derived from photo capture times
            entry["date"] = o.get("date") or fmt_span(o_span)
            entry["photos"] = photos
            col_span = widen(widen(col_span, o_span[0]), o_span[1])
            outings_out.append(entry)

        if not outings_out:
            continue
        # cover fallback = first photo's thumbnail (JS randomises it per load)
        first = outings_out[0]["photos"][0] if outings_out[0]["photos"] else {}
        cover = first.get("grid", "")
        col = dict(id=c["id"], name=c["name"],
                   dates=c.get("dates") or fmt_span(col_span),
                   kind=c["kind"], cover=cover,
                   cover_focus=first.get("focus", "50% 50%"), outings=outings_out)
        if c.get("region"):                       # optional geographic region for this collection
            col["region"] = c["region"]
        if c["kind"] == "outdoor":
            col["total_distance"] = "%.1f km" % total_km
        # keep the raw span alongside the card: `dates` is a display string,
        # so the newest-first sort below needs the real dates
        collections_out.append((col_span, col))

        stub = os.path.join(ROOT, "_pics", c["id"] + ".md")
        os.makedirs(os.path.dirname(stub), exist_ok=True)
        with open(stub, "w") as f:
            f.write("---\nlayout: collection\ncollection_id: %s\ntitle: %s\n---\n" % (c["id"], c["name"]))

    # newest first: by start date, end date breaks ties (undated collections last)
    collections_out.sort(key=lambda t: (t[0][0] or date.min, t[0][1] or date.min),
                         reverse=True)
    collections_out = [col for _, col in collections_out]

    if not args.dry_run:
        with open(os.path.join(ROOT, "_data", "collections.json"), "w") as f:
            json.dump(collections_out, f, indent=2, ensure_ascii=False)
            f.write("\n")
        save_cache(fresh, cache)
        focus_mod.save_store(focus_store)
    print("Done: %d collections (%d photos reused from cache, %d downloaded)."
          % (len(collections_out), reused, fetched))

if __name__ == "__main__":
    main()
