#!/usr/bin/env python3
"""Download GPX tracks + activity stats from Garmin Connect.

Reads `_data/trips.yml`; for every outing linked to Garmin activities it
  * downloads their GPX to the outing's `gpx:` path (several ids are joined
    into one track), and
  * caches Garmin's own activity summaries in `_data/garmin_stats.json`.

Run this BEFORE tools/sync_immich.py. The sync uses the cached Garmin
**elevation gain/loss** in preference to summing GPX `<ele>` deltas — barometric
altimeter figures, already smoothed by Garmin, instead of noisy per-point GPS
elevation that inflates ascent badly on long tracks. Distance and moving time
still come from the GPX. Outings with no cached Garmin stats fall back to the
GPX numbers, so nothing breaks if you never run this.

Add the id to an outing like so (it's the number in the Connect activity URL,
connect.garmin.com/modern/activity/<id>):

    - id: gr54
      name: "Grand Tour des Écrins"
      activity: hike
      gpx: tracks/gr54.gpx        # where this script writes it
      garmin_activity: 12345678901

If an outing's track spans several Garmin activities (two rides in one day, a
watch restarted mid-hike), leave `garmin_activity:` off and list the ids under
`garmin_activities:` instead — each one's GPX is downloaded and they're joined,
in time order, into a single track at the outing's `gpx:` path. Their elevation
gain/loss is summed too:

    - id: gr54_2
      gpx: tracks/gr54/day_2.gpx          # written as one joined track
      garmin_activities: [12345678901, 12345678902]

An existing file is left alone unless --force is passed, so a hand-edited merge
survives (drop the ids to a comment if you never want it rewritten).

Auth: the first run prompts for your email + password (password hidden), logs
in, and caches the session in GARMINTOKENS (default ~/.garminconnect). Every run
after that reuses the cached token — no credentials needed. For non-interactive
use (cron/CI) you can instead set GARMIN_EMAIL / GARMIN_PASSWORD and they'll be
used without prompting. Override the cache dir with GARMINTOKENS.

Usage:
    python3 tools/fetch_garmin.py            # fetch missing tracks + missing stats
    python3 tools/fetch_garmin.py --force    # re-download/refresh everything
    python3 tools/fetch_garmin.py --stats    # only refresh the stats cache

Dependencies:  pip install garminconnect pyyaml

NOTE: python-garminconnect is an UNOFFICIAL client and can break when Garmin
changes their login flow. If auth fails, upgrade it: pip install -U garminconnect
"""
import argparse, getpass, json, os, sys, xml.etree.ElementTree as ET

try:
    import yaml
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    sys.exit("Missing deps: pip install garminconnect pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = os.path.join(ROOT, "tracks")
STATS_FILE = os.path.join(ROOT, "_data", "garmin_stats.json")
GARMINTOKENS = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))

GPX_NS = "http://www.topografix.com/GPX/1/1"
# Garmin's per-point extensions (heart rate, cadence) ride along in the merge;
# registering the usual prefixes keeps the joined file readable.
ET.register_namespace("", GPX_NS)          # <trkpt> etc. stay unprefixed
ET.register_namespace("gpxtpx", "http://www.garmin.com/xmlschemas/TrackPointExtension/v1")
ET.register_namespace("gpxx", "http://www.garmin.com/xmlschemas/GpxExtensions/v3")


def make_client():
    """Log in, preferring cached tokens; fall back to email/password (+ MFA)."""
    try:
        g = Garmin()
        g.login(GARMINTOKENS)                 # reuse cached session
        return g
    except Exception:
        pass                                   # no/expired tokens -> full login

    # Prompt once (password hidden); env vars used only if set (non-interactive).
    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    g = Garmin(email=email, password=password, return_on_mfa=True)
    res1, res2 = g.login()
    if res1 == "needs_mfa":
        code = input("Garmin MFA code: ").strip()
        g.resume_login(res2, code)
    try:
        g.garth.dump(GARMINTOKENS)             # cache session for next time
        print("Cached Garmin session in %s" % GARMINTOKENS)
    except Exception as e:
        print("(could not cache session: %s)" % e)
    return g


def outings_with_garmin(manifest):
    """Yield (collection_id, outing, [activity ids]) for every Garmin-linked outing.

    Both `garmin_activity:` (one id) and `garmin_activities:` (a list) count —
    every id contributes to the summed elevation.
    """
    for r in manifest.get("collections", []):
        for o in r.get("outings", []):
            ids = [o["garmin_activity"]] if o.get("garmin_activity") else []
            ids += list(o.get("garmin_activities") or [])
            if ids:
                yield r["id"], o, ids


def gpx_ids(outing):
    """The activity ids making up this outing's track.

    `garmin_activity:` names the one activity downloaded as-is. An outing with
    only `garmin_activities:` gets all of them downloaded and joined into a
    single GPX — one day split across several recordings.
    """
    if outing.get("garmin_activity"):
        return [outing["garmin_activity"]]
    return list(outing.get("garmin_activities") or [])


def track_path(o):
    # honour the manifest's gpx: path, else default to tracks/<id>.gpx
    rel = o.get("gpx") or ("tracks/%s.gpx" % o["id"])
    return os.path.join(ROOT, rel.lstrip("/"))


def merge_gpx(blobs, name):
    """Join downloaded GPX files into one track: every `<trkseg>`, in time order.

    Segments are kept whole and simply strung together under a single `<trk>`,
    so points (and their heart-rate/cadence extensions) survive untouched and
    the gap between two recordings stays a gap rather than an invented point.
    """
    segs = []
    for blob in blobs:
        for seg in ET.fromstring(blob).iterfind(".//{%s}trkseg" % GPX_NS):
            t = seg.find("{%s}trkpt/{%s}time" % (GPX_NS, GPX_NS))
            segs.append(("" if t is None else (t.text or ""), seg))
    segs.sort(key=lambda s: s[0])          # stable: untimed segs keep their order

    gpx = ET.Element("{%s}gpx" % GPX_NS, {"version": "1.1", "creator": "trips merge"})
    trk = ET.SubElement(gpx, "{%s}trk" % GPX_NS)
    if name:
        ET.SubElement(trk, "{%s}name" % GPX_NS).text = name
    for _, seg in segs:
        trk.append(seg)
    return ET.tostring(gpx, encoding="UTF-8", xml_declaration=True)


# ---------------- activity stats cache ----------------
def load_stats():
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def save_stats(stats):
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2, sort_keys=True)
        f.write("\n")


def summarise(activity):
    """Pull the numbers we care about out of a Garmin activity summary.

    Garmin nests them under `summaryDTO`; some endpoints/versions put them at the
    top level instead, so check both. Elevation is in metres, duration seconds.
    """
    dto = activity.get("summaryDTO") or {}

    def pick(*keys):
        for k in keys:
            v = dto.get(k, activity.get(k))
            if v is not None:
                return float(v)
        return None

    return dict(
        name=(activity.get("activityName")
              or (activity.get("activityId") and str(activity["activityId"]))),
        ascent_m=pick("elevationGain"),
        descent_m=pick("elevationLoss"),
        distance_m=pick("distance"),
        moving_s=pick("movingDuration", "duration", "elapsedDuration"),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download tracks and refresh cached stats")
    ap.add_argument("--stats", action="store_true",
                    help="only refresh the activity stats cache (no GPX downloads)")
    args = ap.parse_args()

    manifest = yaml.safe_load(open(os.path.join(ROOT, "_data", "trips.yml")))
    jobs = list(outings_with_garmin(manifest))
    if not jobs:
        print("No outings have a `garmin_activity:`/`garmin_activities:` id — nothing to fetch.")
        return

    stats = load_stats()
    # tracks: one activity written as-is, several joined into a single GPX
    tracks_todo = [] if args.stats else [
        (rid, o, gpx_ids(o)) for rid, o, _ in jobs
        if gpx_ids(o) and (args.force or not os.path.exists(track_path(o)))
    ]
    # stats: every linked activity id we don't already have cached
    stats_todo = []
    for rid, o, ids in jobs:
        for act_id in ids:
            if (args.force or str(act_id) not in stats) and act_id not in stats_todo:
                stats_todo.append(act_id)

    if not tracks_todo and not stats_todo:
        print("All %d Garmin outing(s) up to date (use --force to refresh)." % len(jobs))
        return

    os.makedirs(TRACKS, exist_ok=True)
    client = make_client()

    def guard(what, fn):
        """Run a Garmin call, turning auth failure into a hard exit."""
        try:
            return fn()
        except GarminConnectAuthenticationError:
            sys.exit("Authentication failed — delete %s and retry with GARMIN_EMAIL/PASSWORD."
                     % GARMINTOKENS)
        except Exception as e:
            print("  ! %s failed: %s" % (what, e))
            return None

    ok = 0
    for rid, o, ids in tracks_todo:
        dest = track_path(o)
        blobs = []
        for act_id in ids:
            data = guard("%s/%s: activity %s" % (rid, o["id"], act_id),
                         lambda act_id=act_id: client.download_activity(
                             act_id, dl_fmt=client.ActivityDownloadFormat.GPX))
            if data is None:
                break                      # partial merge would be a wrong track
            blobs.append(data)
        if len(blobs) != len(ids):
            print("  ! %s/%s: skipped, only %d of %d activities downloaded"
                  % (rid, o["id"], len(blobs), len(ids)))
            continue

        if len(blobs) == 1:
            data = blobs[0]
        else:
            try:
                data = merge_gpx(blobs, o.get("name") or o["id"])
            except ET.ParseError as e:
                print("  ! %s/%s: could not join GPX files: %s" % (rid, o["id"], e))
                continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        print("  %s/%s: activit%s %s -> %s"
              % (rid, o["id"], "y" if len(ids) == 1 else "ies",
                 " + ".join(str(i) for i in ids), os.path.relpath(dest, ROOT)))
        ok += 1

    got = 0
    for act_id in stats_todo:
        a = guard("stats for activity %s" % act_id, lambda: client.get_activity(act_id))
        if a is None:
            continue
        st = summarise(a)
        stats[str(act_id)] = st
        print("  stats %s: \u2191%s m \u2193%s m (%s)" % (
            act_id,
            "?" if st["ascent_m"] is None else int(round(st["ascent_m"])),
            "?" if st["descent_m"] is None else int(round(st["descent_m"])),
            st["name"] or "-"))
        got += 1
    if got:
        save_stats(stats)
        print("Cached stats for %d activity(ies) in %s"
              % (got, os.path.relpath(STATS_FILE, ROOT)))

    print("Fetched %d/%d track(s), %d/%d stat(s). Now run: python3 tools/sync_immich.py"
          % (ok, len(tracks_todo), got, len(stats_todo)))


if __name__ == "__main__":
    main()
