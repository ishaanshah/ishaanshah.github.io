#!/usr/bin/env python3
"""Generate sample Trips data + placeholder assets so the gallery renders
end-to-end without a live Immich instance.

Outputs (all overwritten each run):
  tracks/<outing>.gpx                     synthetic GPX tracks
  assets/trips/sample/<...>.svg           placeholder photos + covers
  _data/collections.json                  materialised data consumed by Jekyll
  _pics/<collection>.md                   collection stubs -> /pics/<collection>/

The real pipeline (tools/sync_immich.py) writes the SAME collections.json schema,
sourcing photos + EXIF GPS from Immich and stats from the GPX files.
"""
import json, math, os, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = os.path.join(ROOT, "tracks")
IMG = os.path.join(ROOT, "assets", "trips", "sample")
DATA = os.path.join(ROOT, "_data")
PICS_DIR = os.path.join(ROOT, "_pics")
for d in (TRACKS, IMG, DATA, PICS_DIR):
    os.makedirs(d, exist_ok=True)

ROUTE_COLORS = ["#3d898d", "#c26b45", "#7d5ea3", "#4f8f5b", "#3f78a8", "#b78a2e"]
CAPTIONS = ["First light", "Coffee stop", "Above the clouds", "The way up",
            "Golden hour", "Looking back", "Quiet corner", "Down in the valley",
            "Old stonework", "Water crossing", "Almost there", "Last light"]

# ---- placeholder photo SVGs (layered mountains) ----
MOODS = [
    (("#cfe6ef", "#eef6f4"), "#fff6e6", ("#7f9aa1", "#5c7b81", "#3d585e")),
    (("#d8ead4", "#f1f6ec"), "#fbf3d8", ("#7a9a72", "#557551", "#385239")),
    (("#f7ddc9", "#fbeede"), "#ffe9c9", ("#c98f6f", "#9c6a55", "#5f4640")),
    (("#d8d1e8", "#efe9f2"), "#f3e3ef", ("#8079a0", "#5c567b", "#3d3a56")),
    (("#d3e7ee", "#eef6f8"), "#eaf6fb", ("#84a6af", "#5f858f", "#41666f")),
]

def ridge(rng, base, amp, y0):
    n, d = 7, "M0,180 "
    for i in range(n + 1):
        x = i * (160 / n)
        y = max(10, y0 - (rng.random() * amp + base) + math.sin(i * 1.7) * amp * .3)
        d += "L%.1f,%.1f " % (x, y)
    return d + "L160,180 Z"

def mountain_svg(seed):
    rng = random.Random(seed)
    sky, sun, peaks = rng.choice(MOODS)
    gid = "g%d" % (seed & 0xffffff)
    circle = '<circle cx="%d" cy="%d" r="%d" fill="%s" opacity=".85"/>' % (
        30 + int(rng.random() * 100), 25 + int(rng.random() * 30),
        12 + int(rng.random() * 10), sun)
    layers = "".join('<path d="%s" fill="%s"/>' % (ridge(rng, b, a, y), peaks[i])
                     for i, (b, a, y) in enumerate([(55, 30, 150), (35, 42, 158), (10, 50, 168)]))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 120" '
            'preserveAspectRatio="xMidYMid slice">'
            '<defs><linearGradient id="%s" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/>'
            '</linearGradient></defs><rect width="160" height="120" fill="url(#%s)"/>'
            '%s%s</svg>') % (gid, sky[0], sky[1], gid, circle, layers)

def write_photo(name, seed):
    path = os.path.join(IMG, name + ".svg")
    with open(path, "w") as f:
        f.write(mountain_svg(seed))
    return "/assets/trips/sample/%s.svg" % name

# ---- synthetic GPX + stats ----
def haversine(a, b):
    R, tr = 6371000.0, math.pi / 180
    dlat = (b[0] - a[0]) * tr; dlng = (b[1] - a[1]) * tr
    h = (math.sin(dlat / 2) ** 2 +
         math.cos(a[0] * tr) * math.cos(b[0] * tr) * math.sin(dlng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

def make_track(seed, base_lat, base_lng, base_ele, npts=70):
    rng = random.Random(seed)
    lat, lng = base_lat, base_lng
    hdg = rng.random() * 2 * math.pi
    pts = []
    for i in range(npts):
        hdg += (rng.random() - 0.5) * 1.1
        step = 0.0012 + rng.random() * 0.0014
        lat += math.sin(hdg) * step
        lng += math.cos(hdg) * step
        # a hump-shaped elevation profile with noise
        frac = i / (npts - 1)
        ele = base_ele + math.sin(frac * math.pi) * (500 + rng.random() * 700) \
              + math.sin(frac * math.pi * 4 + seed) * 60 + rng.random() * 30
        pts.append((round(lat, 6), round(lng, 6), round(ele, 1)))
    return pts

def gpx_text(name, pts):
    body = "\n".join('   <trkpt lat="%f" lon="%f"><ele>%.1f</ele></trkpt>' % (p[0], p[1], p[2])
                     for p in pts)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="gen_sample.py" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
            ' <trk><name>%s</name><trkseg>\n%s\n </trkseg></trk>\n</gpx>\n') % (name, body)

def track_stats(pts, kmh):
    dist = sum(haversine(pts[i - 1], pts[i]) for i in range(1, len(pts)))
    ascent = sum(max(0.0, pts[i][2] - pts[i - 1][2]) for i in range(1, len(pts)))
    hours = (dist / 1000.0) / kmh
    h, m = int(hours), int(round((hours - int(hours)) * 60))
    if m == 60:
        h, m = h + 1, 0
    return ("%.1f km" % (dist / 1000.0),
            "%d m" % (int(round(ascent / 10.0)) * 10),
            "%dh %02dm" % (h, m),
            dist / 1000.0)

# ---- collection definitions (curated bits only) ----
SPEED = {"hike": 3.6, "bike": 15.0, "run": 9.5}
COLLECTIONS_SRC = [
    dict(id="mercantour", name="Mercantour",
         region="Mercantour National Park, Alpes-Maritimes", dates="Jul–Sep 2025",
         kind="outdoor", base=(44.075, 7.435, 1900),
         outings=[
             dict(id="merveilles", name="Vallée des Merveilles", activity="hike", date="Sep 2025", nph=4),
             dict(id="begoridge", name="Mont Bégo Ridge", activity="hike", date="Aug 2025", nph=3),
         ]),
    dict(id="esterel", name="Estérel",
         region="Estérel Massif, Var", dates="Apr–May 2025",
         kind="outdoor", base=(43.485, 6.87, 60),
         outings=[
             dict(id="redrock", name="Red Rock Loop", activity="bike", date="May 2025", nph=4),
             dict(id="caproux", name="Cap Roux Trail", activity="run", date="Apr 2025", nph=3),
         ]),
    dict(id="venice", name="Venice",
         region="Veneto, Italy", dates="Jul 2025", kind="city", base=(45.44, 12.33, 0),
         outings=[
             dict(id="cannaregio", name="Cannaregio & the Ghetto", activity="city", date="Day 1", nph=3),
             dict(id="sanmarco", name="San Marco at dawn", activity="city", date="Day 2", nph=3),
         ]),
]

def build():
    collections_out = []
    cap = 0
    for r in COLLECTIONS_SRC:
        blat, blng, bele = r["base"]
        outings_out = []
        total_km = 0.0
        for oi, o in enumerate(r["outings"]):
            color = ROUTE_COLORS[oi % len(ROUTE_COLORS)]
            seed = abs(hash(o["id"])) & 0x7fffffff
            photos = []
            entry = dict(id=o["id"], name=o["name"], activity=o["activity"],
                         date=o["date"], color=color)
            if r["kind"] == "outdoor":
                pts = make_track(seed, blat + oi * 0.02, blng + oi * 0.02, bele)
                with open(os.path.join(TRACKS, o["id"] + ".gpx"), "w") as f:
                    f.write(gpx_text(o["name"], pts))
                dist, ascent, dur, km = track_stats(pts, SPEED.get(o.get("activity"), 4.0))
                total_km += km
                entry.update(gpx="/tracks/%s.gpx" % o["id"],
                             distance=dist, ascent=ascent, duration=dur)
                # place photos at points spread along the track
                for i in range(o["nph"]):
                    idx = int((i + 0.6) / o["nph"] * (len(pts) - 1))
                    src = write_photo("%s-%d" % (o["id"], i), seed + i * 97 + 13)
                    photos.append(dict(grid=src, full=src, caption=CAPTIONS[cap % len(CAPTIONS)],
                                       lat=pts[idx][0], lng=pts[idx][1], width=1600, height=1067))
                    cap += 1
            else:
                for i in range(o["nph"]):
                    src = write_photo("%s-%d" % (o["id"], i), seed + i * 97 + 13)
                    photos.append(dict(grid=src, full=src, caption=CAPTIONS[cap % len(CAPTIONS)],
                                       width=1600, height=1067))
                    cap += 1
            entry["photos"] = photos
            outings_out.append(entry)

        cover = write_photo("cover-%s" % r["id"], abs(hash(r["id"])) & 0x7fffffff)
        col = dict(id=r["id"], name=r["name"], region=r["region"], dates=r["dates"],
                   kind=r["kind"], cover=cover, outings=outings_out)
        if r["kind"] == "outdoor":
            col["total_distance"] = "%.1f km" % total_km
        collections_out.append(col)

        with open(os.path.join(PICS_DIR, r["id"] + ".md"), "w") as f:
            f.write("---\nlayout: collection\ncollection_id: %s\ntitle: %s\n---\n" % (r["id"], r["name"]))

    with open(os.path.join(DATA, "collections.json"), "w") as f:
        json.dump(collections_out, f, indent=2, ensure_ascii=False)
    print("Wrote %d collections, sample tracks + placeholder photos." % len(collections_out))

if __name__ == "__main__":
    build()
