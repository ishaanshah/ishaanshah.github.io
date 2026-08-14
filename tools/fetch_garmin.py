#!/usr/bin/env python3
"""Download GPX tracks from Garmin Connect into tracks/.

Reads `_data/trips.yml`; for every outing that has a `garmin_activity:` id it
downloads that activity's GPX to `tracks/<outing>.gpx` (the path the manifest's
`gpx:` field points at). Run this BEFORE tools/sync_immich.py, which then reads
the tracks to compute distance / ascent / duration.

Add the id to an outing like so (it's the number in the Connect activity URL,
connect.garmin.com/modern/activity/<id>):

    - id: gr54
      name: "Grand Tour des Écrins"
      activity: hike
      gpx: tracks/gr54.gpx        # where this script writes it
      garmin_activity: 12345678901

Auth: the first run prompts for your email + password (password hidden), logs
in, and caches the session in GARMINTOKENS (default ~/.garminconnect). Every run
after that reuses the cached token — no credentials needed. For non-interactive
use (cron/CI) you can instead set GARMIN_EMAIL / GARMIN_PASSWORD and they'll be
used without prompting. Override the cache dir with GARMINTOKENS.

Usage:
    python3 tools/fetch_garmin.py            # fetch missing tracks
    python3 tools/fetch_garmin.py --force    # re-download even if the file exists

Dependencies:  pip install garminconnect pyyaml

NOTE: python-garminconnect is an UNOFFICIAL client and can break when Garmin
changes their login flow. If auth fails, upgrade it: pip install -U garminconnect
"""
import argparse, getpass, os, sys

try:
    import yaml
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    sys.exit("Missing deps: pip install garminconnect pyyaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = os.path.join(ROOT, "tracks")
GARMINTOKENS = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))


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
    for r in manifest.get("regions", []):
        for o in r.get("outings", []):
            if o.get("garmin_activity"):
                yield r["id"], o


def track_path(o):
    # honour the manifest's gpx: path, else default to tracks/<id>.gpx
    rel = o.get("gpx") or ("tracks/%s.gpx" % o["id"])
    return os.path.join(ROOT, rel.lstrip("/"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if the GPX exists")
    args = ap.parse_args()

    manifest = yaml.safe_load(open(os.path.join(ROOT, "_data", "trips.yml")))
    jobs = list(outings_with_garmin(manifest))
    if not jobs:
        print("No outings have a `garmin_activity:` id — nothing to fetch.")
        return

    todo = [(rid, o) for rid, o in jobs if args.force or not os.path.exists(track_path(o))]
    if not todo:
        print("All %d Garmin track(s) already downloaded (use --force to refresh)." % len(jobs))
        return

    os.makedirs(TRACKS, exist_ok=True)
    client = make_client()

    ok = 0
    for rid, o in todo:
        act_id = o["garmin_activity"]
        dest = track_path(o)
        try:
            data = client.download_activity(act_id, dl_fmt=client.ActivityDownloadFormat.GPX)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            rel = os.path.relpath(dest, ROOT)
            print("  %s/%s: activity %s -> %s" % (rid, o["id"], act_id, rel))
            ok += 1
        except GarminConnectAuthenticationError:
            sys.exit("Authentication failed — delete %s and retry with GARMIN_EMAIL/PASSWORD." % GARMINTOKENS)
        except Exception as e:
            print("  ! %s/%s: activity %s failed: %s" % (rid, o["id"], act_id, e))

    print("Fetched %d/%d track(s). Now run: python3 tools/sync_immich.py" % (ok, len(todo)))


if __name__ == "__main__":
    main()
