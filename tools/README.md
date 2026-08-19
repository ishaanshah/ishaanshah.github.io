# Trips gallery pipeline

The Trips gallery is **data-driven and statically served**. Immich is the source
of truth for photos; the live site only serves pre-generated static WebP + GPX,
so it never depends on the Immich instance being online.

```
_data/trips.yml (you edit)                    tools/fetch_garmin.py
        │                                             │
        │                        tracks/*.gpx  +  _data/garmin_stats.json
        │                                             │
        └──────────►  tools/sync_immich.py  ◄──────────┘
                              │  (pulls photos + EXIF GPS from Immich, downloads
                              │   WebP derivatives, computes distance/time from
                              │   GPX, ascent/descent from Garmin)
                              ▼
        _data/collections.json  +  _pics/*.md  +  assets/trips/<collection>/<outing>/*.webp
                              │
                              ▼
                     jekyll build → static site
```

## Files
- **`_data/trips.yml`** — the only file you hand-edit. Collections → outings, each
  outing pointing at an Immich album (`immich_album`) and, for outdoor trips, a
  `gpx:` track under `tracks/`.
- **`tools/sync_immich.py`** — pulls from Immich and writes the materialised
  `_data/collections.json` (+ `_pics/*.md` page stubs and the WebP derivatives).
  **Do not hand-edit `collections.json`** — it is regenerated.
- **`_data/garmin_stats.json`** — activity summaries cached by `fetch_garmin.py`
  (ascent, descent, distance, moving time per activity id). Generated, but
  **commit it**: the sync reads it, so nobody needs Garmin credentials to build.
- **`tools/gen_sample.py`** — generates placeholder sample data/assets so the
  gallery renders without Immich. Run once to preview; the real sync overwrites it.

## GPX tracks
Two ways to get an outing's track into `tracks/`:

- **Manual:** Garmin Connect → activity → gear icon → *Export to GPX*, save as
  `tracks/<outing>.gpx`. Reliable, no code.
- **Automated (`tools/fetch_garmin.py`):** add `garmin_activity: <id>` to the outing
  (the number in the Connect activity URL) and run the fetcher — it downloads each
  activity's GPX to the outing's `gpx:` path. Uses the unofficial `garminconnect`
  client; tokens are cached after the first login so MFA is a one-time prompt.

```bash
pip install garminconnect
python3 tools/fetch_garmin.py             # prompts for login on first run, then
                                          # reuses the cached session (--force to refresh)
python3 tools/fetch_garmin.py --stats     # refresh only the stats cache
```

## Elevation
Ascent and descent come from **Garmin's activity summary**, not from the GPX.
Summing `<ele>` deltas over a track adds up per-point GPS noise as well as real
climbing, which overstates a long day's ascent by a wide margin; Garmin's figure
is barometric and already smoothed. `fetch_garmin.py` caches those numbers in
`_data/garmin_stats.json` and the sync prefers them, printing a note and falling
back to the GPX sum for any outing with no cached figure. Distance and moving
time still come from the GPX.

An outing whose GPX is a **hand-merged** track (e.g. two Garmin activities in one
day) should leave `garmin_activity:` off so the merge isn't overwritten, and list
the ids under `garmin_activities:` instead — no GPX is downloaded for those, and
their gains are summed:

```yaml
- id: gr54_2
  gpx: tracks/gr54/day_2.gpx            # manual merge, left alone
  garmin_activities: [12345678901, 12345678902]
```
The first run asks for your Garmin email/password (hidden) and caches the session
in `~/.garminconnect`; later runs need no credentials. For cron/CI you can set
`GARMIN_EMAIL` / `GARMIN_PASSWORD` to skip the prompt.

## First-time setup
```bash
pip install requests pyyaml pillow

export IMMICH_URL=https://photos.example.com     # no trailing /api
export IMMICH_KEY=<your Immich API key>          # Account Settings → API Keys

python3 tools/fetch_garmin.py             # optional: pull GPX + elevation from Garmin
python3 tools/sync_immich.py --albums     # list album names/UUIDs to fill trips.yml
# edit _data/trips.yml to match your collections + albums
python3 tools/sync_immich.py --dry-run    # resolve + report, downloads nothing
python3 tools/sync_immich.py              # full sync → collections.json + WebP
jekyll build                              # or: jekyll serve
git add -A && git commit                  # commit the generated static assets
```

## Notes
- Photo pins are placed from each photo's EXIF GPS; photos without coordinates
  still appear in the grid, just without a map pin. Photo captions come from the
  Immich asset **description**.
- Immich REST paths have changed across versions; this targets the
  `/api/albums` + `/api/assets/{id}/thumbnail` shape (Immich ≥ ~1.94). Adjust
  `ENDPOINTS` in `sync_immich.py` if your instance 404s.
- The sample `tracks/` and `assets/trips/sample/` files are placeholders — delete
  them once real data is synced (and remove the sample entries from `collections.json`,
  which the sync does automatically when you run it for real).
- Repo bloat: only downscaled WebP are committed (grid ≤800px, lightbox ≤1800px).
  If the gallery grows large, consider moving `assets/trips/` to a separate branch
  or object store.
