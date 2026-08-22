#!/usr/bin/env python3
"""Geotag Immich photos from an outing's GPX track.

Phone photos carry GPS; a camera's usually don't. This walks an outing's Immich
album, and for every photo with no coordinates works out where the GPX track was
at the moment the shutter fired — interpolating between the two trackpoints that
bracket it — then writes that position back to Immich.

Albums and tracks are resolved from `_data/trips.yml`, so you name an outing (or
a whole collection, or an album) rather than repeating paths:

  python3 tools/geotag_immich.py eze                 # one outing
  python3 tools/geotag_immich.py cote_dazur          # every outing in a collection
  python3 tools/geotag_immich.py "Èze Hiking"        # by Immich album name
  python3 tools/geotag_immich.py --album "X" --gpx tracks/x.gpx    # ad-hoc, no manifest

Nothing is written without `--apply`; the default is a dry run that prints the
match it would make for each photo. Once applied, re-run tools/sync_immich.py to
pull the new coordinates into the site's data.

Times are matched as absolute instants: GPX is UTC, and Immich returns a
timezone-aware capture time. A camera whose clock was wrong (or that recorded no
timezone) shifts every photo by the same amount — `--offset` corrects it, and a
dry run that lands outside the track suggests the whole-hour shift that fits.

Config via environment (same as sync_immich.py):
  IMMICH_URL   e.g. https://photos.example.com   (no trailing /api)
  IMMICH_KEY   an API key with asset.read, asset.update and album.read

Dependencies:  requests, pyyaml   (pip install requests pyyaml)
"""
import argparse, bisect, os, sys, xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    import requests, yaml
except ImportError:
    sys.exit("Missing deps: pip install requests pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRIPS = os.path.join(ROOT, "_data", "trips.yml")

IMMICH_URL = os.environ.get("IMMICH_URL", "").rstrip("/")
IMMICH_KEY = os.environ.get("IMMICH_KEY", "")
S = requests.Session()
S.headers.update({"x-api-key": IMMICH_KEY, "Accept": "application/json"})

# ---------------- Immich ----------------
def api(method, path, **kw):
    r = S.request(method, IMMICH_URL + path, timeout=60, **kw)
    r.raise_for_status()
    return r.json() if r.content else None

def list_albums():
    return api("GET", "/api/albums")

def resolve_album(ident, albums):
    for a in albums:
        if a.get("id") == ident or a.get("albumName") == ident:
            return a["id"]
    return None

def album_assets(album_id):
    full = api("GET", "/api/albums/" + album_id)
    return [a for a in full.get("assets", []) if a.get("type") == "IMAGE"]

def set_location(asset_id, lat, lng):
    api("PUT", "/api/assets/" + asset_id,
        json={"latitude": round(lat, 6), "longitude": round(lng, 6)})

def has_gps(a):
    ex = a.get("exifInfo") or {}
    return ex.get("latitude") is not None and ex.get("longitude") is not None

# ---------------- time ----------------
def parse_iso(s):
    """ISO-8601 -> aware datetime, or None. Naive input is read as UTC."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def asset_time(a):
    """When the shutter fired, as an absolute instant (epoch seconds), or None.

    `dateTimeOriginal` is what the camera recorded and what Immich keeps
    timezone-aware; `localDateTime` is a wall-clock fallback with no zone, so it
    is only right for a camera that was set to UTC — hence the --offset knob.
    """
    ex = a.get("exifInfo") or {}
    dt = parse_iso(ex.get("dateTimeOriginal")) or parse_iso(a.get("localDateTime"))
    return dt.timestamp() if dt else None

def fmt_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def fmt_delta(sec):
    sec = int(round(sec))
    sign = "-" if sec < 0 else "+"
    sec = abs(sec)
    if sec < 60:
        return "%s%ds" % (sign, sec)
    if sec < 3600:
        return "%s%dm%02ds" % (sign, sec // 60, sec % 60)
    return "%s%dh%02dm" % (sign, sec // 3600, (sec % 3600) // 60)

# ---------------- GPX ----------------
def parse_gpx(path):
    """[(epoch, lat, lon)] from every timed trackpoint, in time order.

    Namespaces are stripped rather than matched: Garmin writes GPX 1.1, but
    hand-merged or third-party tracks turn up as 1.0 often enough to matter.
    """
    root = ET.parse(path).getroot()
    pts = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        t = None
        for child in el:
            if child.tag.rsplit("}", 1)[-1] == "time":
                t = parse_iso(child.text)
                break
        if t is None or el.get("lat") is None or el.get("lon") is None:
            continue
        pts.append((t.timestamp(), float(el.get("lat")), float(el.get("lon"))))
    pts.sort(key=lambda p: p[0])
    return pts

def locate(pts, times, ts, tolerance):
    """Where the track was at `ts`: (lat, lon, gap seconds), or None.

    Between two trackpoints the position is interpolated linearly in time. `gap`
    is the distance to the nearest *recorded* point, which is what tolerance is
    judged on — it stays near zero inside a normally-sampled track and grows
    across a paused recording or beyond either end, exactly the cases where an
    interpolated position would be a guess.
    """
    i = bisect.bisect_left(times, ts)
    if i == 0:
        gap = times[0] - ts
        return (pts[0][1], pts[0][2], gap) if gap <= tolerance else None
    if i == len(times):
        gap = ts - times[-1]
        return (pts[-1][1], pts[-1][2], gap) if gap <= tolerance else None
    t0, lat0, lon0 = pts[i - 1]
    t1, lat1, lon1 = pts[i]
    gap = min(ts - t0, t1 - ts)
    if gap > tolerance:
        return None
    f = 0.0 if t1 == t0 else (ts - t0) / (t1 - t0)
    return (lat0 + (lat1 - lat0) * f, lon0 + (lon1 - lon0) * f, gap)

def suggest_offset(times, stamps):
    """Whole-hour --offset that would drop the most photos onto the track.

    A camera left on the wrong timezone misses by a round number of hours, and
    every photo by the same amount, so this is nearly always the fix when a whole
    album falls outside its track.
    """
    lo, hi, best = times[0], times[-1], (0, 0)
    for hours in range(-14, 15):
        if hours == 0:
            continue
        shift = hours * 3600
        n = sum(1 for ts in stamps if lo <= ts + shift <= hi)
        if n > best[1]:
            best = (hours, n)
    return best if best[1] else None

# ---------------- manifest ----------------
def load_outings(selectors):
    """Manifest outings matching each selector, as (label, album, gpx path).

    A selector is an outing id, a collection id (all of its outings), or an
    Immich album name — whichever it matches first.
    """
    manifest = yaml.safe_load(open(TRIPS))
    chosen, known = [], []
    for c in manifest["collections"]:
        for o in c["outings"]:
            known.append((c["id"], o))
    for sel in selectors:
        hits = [(cid, o) for cid, o in known if o["id"] == sel] \
            or [(cid, o) for cid, o in known if cid == sel] \
            or [(cid, o) for cid, o in known if o.get("immich_album") == sel]
        if not hits:
            sys.exit("No outing, collection or album named %r in %s.\nKnown outings: %s"
                     % (sel, os.path.relpath(TRIPS, ROOT),
                        ", ".join(sorted(o["id"] for _, o in known))))
        for cid, o in hits:
            if not o.get("gpx"):
                print("  ! %s/%s has no gpx: in the manifest — skipping" % (cid, o["id"]))
                continue
            chosen.append(("%s/%s" % (cid, o["id"]), o["immich_album"],
                           os.path.join(ROOT, o["gpx"])))
    return chosen

# ---------------- main ----------------
def geotag(label, album_name, gpx_path, albums, args):
    """Returns (tagged, skipped_no_match) for one outing."""
    print("\n%s  [%s]" % (label, album_name))
    album_id = resolve_album(album_name, albums)
    if not album_id:
        print("  ! album not found in Immich — skipping")
        return 0, 0
    if not os.path.exists(gpx_path):
        print("  ! no GPX at %s — skipping" % os.path.relpath(gpx_path, ROOT))
        return 0, 0
    pts = parse_gpx(gpx_path)
    if len(pts) < 2:
        print("  ! %s has no timed trackpoints — skipping" % os.path.relpath(gpx_path, ROOT))
        return 0, 0
    times = [p[0] for p in pts]
    print("  track: %d points, %s → %s" % (len(pts), fmt_time(times[0]), fmt_time(times[-1])))

    assets = album_assets(album_id)
    todo = [a for a in assets if args.force or not has_gps(a)]
    print("  album: %d photos, %d to place%s"
          % (len(assets), len(todo), " (--force: re-placing tagged ones too)" if args.force else ""))

    tagged, missed, undated, stamps = 0, [], 0, []
    for a in sorted(todo, key=lambda x: asset_time(x) or 0):
        name = a.get("originalFileName") or a["id"]
        ts = asset_time(a)
        if ts is None:
            print("    - %-28s no capture time — skipped" % name)
            undated += 1
            continue
        ts += args.offset * 60
        stamps.append(ts)
        got = locate(pts, times, ts, args.tolerance)
        if not got:
            if ts < times[0]:
                why = "%s before the track starts" % fmt_delta(times[0] - ts).lstrip("+")
            elif ts > times[-1]:
                why = "%s after the track ends" % fmt_delta(ts - times[-1]).lstrip("+")
            else:
                why = "in a gap in the track"
            print("    - %-28s %s  no trackpoint within %ds (%s)"
                  % (name, fmt_time(ts), args.tolerance, why))
            missed.append(name)
            continue
        lat, lng, gap = got
        print("    %s %-28s %s  → %.6f, %.6f  (%ss from nearest point)"
              % ("✓" if args.apply else "·", name, fmt_time(ts), lat, lng, int(round(gap))))
        if args.apply:
            set_location(a["id"], lat, lng)
        tagged += 1

    if missed and stamps:
        s = suggest_offset(times, stamps)
        if s:
            print("  → %d photo(s) fell outside the track; --offset %d would land %d of "
                  "%d inside it (camera clock off by %dh?)"
                  % (len(missed), s[0] * 60, s[1], len(stamps), s[0]))
    return tagged, len(missed) + undated

def main():
    ap = argparse.ArgumentParser(
        description="Geotag Immich photos that have no GPS from an outing's GPX track.")
    ap.add_argument("selector", nargs="*",
                    help="outing id, collection id, or Immich album name from _data/trips.yml")
    ap.add_argument("--album", help="Immich album name/UUID (ad-hoc, bypasses the manifest)")
    ap.add_argument("--gpx", help="GPX track to use with --album")
    ap.add_argument("--apply", action="store_true",
                    help="write the coordinates to Immich (default: dry run)")
    ap.add_argument("--force", action="store_true",
                    help="also re-place photos that already have GPS")
    ap.add_argument("--tolerance", type=int, default=300, metavar="SEC",
                    help="how far a photo may sit from the nearest trackpoint (default 300)")
    ap.add_argument("--offset", type=float, default=0, metavar="MIN",
                    help="minutes to add to every capture time (camera clock / timezone fix)")
    ap.add_argument("--albums", action="store_true", help="list Immich albums and exit")
    args = ap.parse_args()

    if not IMMICH_URL or not IMMICH_KEY:
        sys.exit("Set IMMICH_URL and IMMICH_KEY environment variables.")
    albums = list_albums()
    if args.albums:
        for a in sorted(albums, key=lambda x: x.get("albumName", "")):
            print("%-40s %s" % (a.get("albumName"), a.get("id")))
        return

    if args.album:
        if not args.gpx:
            sys.exit("--album needs --gpx.")
        outings = [(args.album, args.album, os.path.join(ROOT, args.gpx))]
    elif args.selector:
        outings = load_outings(args.selector)
    else:
        sys.exit("Name an outing, collection or album (or use --album with --gpx). "
                 "See --help.")

    tagged = skipped = 0
    for label, album, gpx in outings:
        t, s = geotag(label, album, gpx, albums, args)
        tagged += t; skipped += s
    print("\n%s %d photo(s)%s, %d left unplaced."
          % ("Geotagged" if args.apply else "Would geotag", tagged,
             "" if args.apply else " — re-run with --apply to write them", skipped))
    if tagged and args.apply:
        print("Now run tools/sync_immich.py to pull the new coordinates into the site.")

if __name__ == "__main__":
    main()
