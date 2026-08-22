#!/usr/bin/env python3
"""Focal points for the gallery crops.

The justified photo grid never crops, but the collection covers (16/10) and the
map hover thumbnails (4/3) do — and since a cover is picked at random from every
photo in the collection, *every* photo needs a sensible focal point or a bird
ends up sliced off the edge.

A focal point is a fraction (fx, fy) of the image that CSS `object-position`
pins to the same relative spot in the crop box, so the subject stays visible at
any aspect ratio.

`_data/focus.json` is the store, keyed by Immich asset id (not path: the sync
renumbers files in place when an album's order shifts, so a path key would
silently drift onto the wrong photo). Each entry records how it was set:

    {"assets": {"<uuid>": {"x": 0.42, "y": 0.31, "src": "auto"}}}

`src: "manual"` entries come from the editor and are NEVER touched again by the
auto pass; `src: "auto"` ones are re-seeded when `--reseed` is passed.

Detection combines three cues, all Pillow-only (no numpy/opencv):
  * frequency-tuned saliency — per-pixel LAB distance from the image mean,
    which lights up a bird against sky or a jacket against scree;
  * local edge energy — keeps landscapes anchored on the ridgeline/foreground
    rather than drifting into empty sky;
  * face boxes from Immich, when the instance is reachable — added into the
    map as weighted bumps rather than as an override, because Immich misses
    plenty of faces and a hard override would happily pin the crop onto a
    background bystander while ignoring the subject filling the foreground.
A centre prior keeps the result from latching onto a vignette corner, and the
centroid is taken over the strongest ~15% of the saliency mass (a full-image
centroid just regresses to the middle).

Usage:
  python3 tools/focus.py --seed            # fill in every photo missing an entry
  python3 tools/focus.py --seed --reseed   # also recompute existing auto entries
  python3 tools/focus.py --edit            # click-to-fix editor on localhost:8777
  python3 tools/focus.py --montage out.png # QA contact sheet of the 16/10 crops
"""
import argparse, json, math, os, sys

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("Missing deps: pip install pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOCUS_STORE = os.path.join(ROOT, "_data", "focus.json")
COLLECTIONS = os.path.join(ROOT, "_data", "collections.json")

WORK_W = 180          # saliency is computed on a downscale this wide
TOP_MASS = 0.15       # centroid over the strongest fraction of saliency mass
CENTER_FLOOR = 0.30   # how much saliency survives at the frame edge
CLAMP = (0.08, 0.92)  # never pin the focus right onto the edge
SNAP = 0.04           # anything this close to centre is just centred


# ---------------- store ----------------
def load_store(path=FOCUS_STORE):
    if not os.path.exists(path):
        return {"version": 1, "assets": {}}
    with open(path) as f:
        d = json.load(f)
    d.setdefault("assets", {})
    return d


def save_store(store, path=FOCUS_STORE):
    with open(path, "w") as f:
        json.dump(store, f, indent=1, sort_keys=True)
        f.write("\n")


def as_css(entry):
    """`{"x":.42,"y":.31}` -> `42% 31%` (the CSS object-position value)."""
    if not entry:
        return "50% 50%"
    return "%.4g%% %.4g%%" % (round(entry["x"] * 100, 1), round(entry["y"] * 100, 1))


# ---------------- saliency ----------------
def _norm(vals):
    """Scale to [0,1], clipping the top 1% so one specular highlight can't
    flatten everything else to zero."""
    if not vals:
        return vals
    s = sorted(vals)
    hi = s[min(len(s) - 1, int(len(s) * 0.99))]
    lo = s[int(len(s) * 0.02)]
    span = hi - lo
    if span <= 0:
        return [0.0] * len(vals)
    return [min(1.0, max(0.0, (v - lo) / span)) for v in vals]


def saliency_map(im):
    """Return (w, h, [float]) — a coarse per-pixel saliency for a PIL image."""
    w, h = im.size
    sw = WORK_W
    sh = max(1, int(round(h * sw / float(w))))
    small = im.convert("RGB").resize((sw, sh), Image.LANCZOS)

    # frequency-tuned saliency: LAB distance between a lightly blurred image
    # and its global mean colour
    lab = small.filter(ImageFilter.GaussianBlur(1.2)).convert("LAB")
    px = list(lab.getdata())
    n = float(len(px))
    ml = sum(p[0] for p in px) / n
    ma = sum(p[1] for p in px) / n
    mb = sum(p[2] for p in px) / n
    ft = [((p[0] - ml) ** 2 + (p[1] - ma) ** 2 + (p[2] - mb) ** 2) ** 0.5 for p in px]

    # local detail: edge magnitude, blurred so a texture patch reads as one blob
    edges = (small.convert("L")
             .filter(ImageFilter.FIND_EDGES)
             .filter(ImageFilter.GaussianBlur(2.5)))
    ed = [float(v) for v in edges.getdata()]

    ft = _norm(ft)
    ed = _norm(ed)
    sal = []
    for i in range(len(ft)):
        x = (i % sw + 0.5) / sw - 0.5
        y = (i // sw + 0.5) / sh - 0.5
        # centre prior: a soft gaussian that never fully zeroes the border
        prior = CENTER_FLOOR + (1.0 - CENTER_FLOOR) * math.exp(-(x * x + y * y) / (2 * 0.30 ** 2))
        sal.append((0.55 * ft[i] + 0.45 * ed[i]) * prior)
    return sw, sh, sal


def add_faces(sw, sh, sal, faces):
    """Add face boxes into the saliency map as bumps weighted by face size.

    `faces` is a list of (x1, y1, x2, y2) in image fractions. A face filling the
    frame swamps everything else; a small one in the background only nudges the
    result, which is what we want given Immich detects the bystander behind the
    subject about as often as the subject."""
    peak = max(sal) or 1.0
    for x1, y1, x2, y2 in faces:
        # a little headroom above the box: a portrait crop wants the whole head
        y1 = max(0.0, y1 - 0.35 * (y2 - y1))
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        if area <= 0:
            continue
        gain = peak * min(1.6, (area / 0.02) ** 0.5)
        for gy in range(int(y1 * sh), min(sh, int(math.ceil(y2 * sh)))):
            for gx in range(int(x1 * sw), min(sw, int(math.ceil(x2 * sw)))):
                sal[gy * sw + gx] += gain


def salient_centroid(im, faces=None):
    """Centroid of the strongest saliency mass, as (fx, fy) fractions."""
    sw, sh, sal = saliency_map(im)
    if faces:
        add_faces(sw, sh, sal, faces)
    cut = sorted(sal, reverse=True)[max(0, int(len(sal) * TOP_MASS) - 1)]
    sx = sy = tot = 0.0
    for i, v in enumerate(sal):
        if v < cut:
            continue
        sx += v * ((i % sw + 0.5) / sw)
        sy += v * ((i // sw + 0.5) / sh)
        tot += v
    if tot <= 0:
        return 0.5, 0.5
    return sx / tot, sy / tot


def compute_focus(path, faces=None):
    """Focal point for one image file, as (fx, fy) in [0,1]."""
    with Image.open(path) as im:
        im.load()
        fx, fy = salient_centroid(im, faces)
    lo, hi = CLAMP
    fx = min(hi, max(lo, fx))
    fy = min(hi, max(lo, fy))
    if abs(fx - 0.5) < SNAP:
        fx = 0.5
    if abs(fy - 0.5) < SNAP:
        fy = 0.5
    return round(fx, 3), round(fy, 3)


# ---------------- faces, from Immich ----------------
def face_boxes(asset_detail):
    """Face boxes as (x1, y1, x2, y2) fractions, from an /api/assets/{id} body.

    Immich reports both faces matched to a named person and `unassignedFaces`;
    for cropping purposes they count the same."""
    raw = list(asset_detail.get("unassignedFaces") or [])
    for person in (asset_detail.get("people") or []):
        raw += person.get("faces") or []
    out = []
    for b in raw:
        iw, ih = b.get("imageWidth") or 0, b.get("imageHeight") or 0
        if not iw or not ih:
            continue
        out.append((b["boundingBoxX1"] / float(iw), b["boundingBoxY1"] / float(ih),
                    b["boundingBoxX2"] / float(iw), b["boundingBoxY2"] / float(ih)))
    return out


def fetch_faces(session, base_url, asset_id):
    """Face boxes for one asset; [] if Immich is unreachable or says nothing."""
    try:
        r = session.get("%s/api/assets/%s" % (base_url.rstrip("/"), asset_id), timeout=30)
        r.raise_for_status()
        return face_boxes(r.json())
    except Exception:
        return []


# ---------------- QA montage ----------------
def crop_to(im, fx, fy, box_w, box_h):
    """The `object-fit: cover` + `object-position` crop, done in Pillow."""
    w, h = im.size
    scale = max(box_w / float(w), box_h / float(h))
    sw, sh = w * scale, h * scale
    # object-position pins the (fx,fy) point of the image to (fx,fy) of the box
    left = (sw - box_w) * fx
    top = (sh - box_h) * fy
    return im.resize((max(1, int(round(sw))), max(1, int(round(sh)))), Image.LANCZOS) \
             .crop((int(left), int(top), int(left) + box_w, int(top) + box_h))


def montage(paths, out, cols=4, cell=(320, 200)):
    """Contact sheet of centred crop (left) vs focal crop (right) per photo."""
    cw, ch = cell
    pad, rows = 6, (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (cw * 2 + pad * 3), rows * (ch + pad * 2 + 12)), "#20262b")
    for i, p in enumerate(paths):
        fx, fy = compute_focus(p)
        with Image.open(p) as im:
            im.load()
            a = crop_to(im, 0.5, 0.5, cw, ch)
            b = crop_to(im, fx, fy, cw, ch)
        x = (i % cols) * (cw * 2 + pad * 3) + pad
        y = (i // cols) * (ch + pad * 2 + 12) + pad
        sheet.paste(a, (x, y))
        sheet.paste(b, (x + cw + pad, y))
    sheet.save(out)
    return out


# ---------------- photo inventory ----------------
def inventory():
    """Every rendered photo, joined to its Immich asset id via the sync cache.

    `collections.json` is what the site renders but carries no asset ids, and
    `immich_cache.json` maps asset id -> the WebP it produced; the join gives a
    stable key for the focus store."""
    with open(COLLECTIONS) as f:
        collections = json.load(f)
    by_grid = {}
    cache_path = os.path.join(ROOT, "_data", "immich_cache.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            for aid, e in (json.load(f).get("assets") or {}).items():
                if e.get("grid"):
                    by_grid[e["grid"]] = aid
    out = []
    for c in collections:
        for o in c["outings"]:
            for p in o["photos"]:
                aid = by_grid.get(p.get("grid"))
                if not aid:
                    continue
                out.append(dict(id=aid, grid=p["grid"], full=p.get("full"),
                                caption=p.get("caption") or "",
                                collection=c["name"], outing=o["name"]))
    return out


def immich_session():
    """A session for face lookups, or None when Immich isn't configured."""
    base = os.environ.get("IMMICH_URL", "").rstrip("/")
    key = os.environ.get("IMMICH_KEY", "")
    if not key:
        key_file = os.path.join(ROOT, "immich_key")
        if os.path.exists(key_file):
            with open(key_file) as f:
                key = f.read().strip()
    if not base or not key:
        return None, None
    try:
        import requests
    except ImportError:
        return None, None
    s = requests.Session()
    s.headers.update({"x-api-key": key, "Accept": "application/json"})
    return s, base


def seed(store, reseed=False, verbose=True):
    """Fill in auto focal points for photos that have none. Returns how many."""
    session, base = immich_session()
    if verbose and not session:
        print("  (Immich not configured — saliency only, no face boxes)")
    done = 0
    for ph in inventory():
        cur = store["assets"].get(ph["id"])
        if cur and (cur.get("src") == "manual" or not reseed):
            continue
        path = os.path.join(ROOT, ph["grid"].lstrip("/"))
        if not os.path.exists(path):
            continue
        faces = fetch_faces(session, base, ph["id"]) if session else []
        fx, fy = compute_focus(path, faces)
        store["assets"][ph["id"]] = dict(x=fx, y=fy, src="auto")
        done += 1
        if verbose:
            print("  %s -> %.0f%% %.0f%%%s" % (ph["grid"], fx * 100, fy * 100,
                                               " (%d face%s)" % (len(faces), "" if len(faces) == 1 else "s") if faces else ""))
    return done


def stamp_collections(store):
    """Write the stored focal points into `_data/collections.json`.

    The sync does this too, but correcting a crop shouldn't mean a full Immich
    round-trip — the editor calls this on every save so a `jekyll serve` picks
    the change straight up."""
    with open(COLLECTIONS) as f:
        collections = json.load(f)
    by_grid = {p["grid"]: p["id"] for p in inventory()}
    n = 0
    for c in collections:
        # the card paints `cover` before the randomiser swaps it in
        c["cover_focus"] = as_css(store["assets"].get(by_grid.get(c.get("cover"))))
        for o in c["outings"]:
            for p in o["photos"]:
                aid = by_grid.get(p.get("grid"))
                css = as_css(store["assets"].get(aid)) if aid else "50% 50%"
                if p.get("focus") != css:
                    p["focus"] = css
                    n += 1
    with open(COLLECTIONS, "w") as f:
        json.dump(collections, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return n


# ---------------- editor ----------------
def serve_editor(port=8777):
    """Click-to-fix editor: a local page over the real photos, saving straight
    into _data/focus.json (no download-and-move dance)."""
    import http.server, socketserver, urllib.parse, webbrowser

    editor_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "focus_editor.html")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(editor_html, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/photos":
                store = load_store()
                photos = []
                for ph in inventory():
                    e = store["assets"].get(ph["id"]) or {}
                    photos.append(dict(ph, x=e.get("x", 0.5), y=e.get("y", 0.5),
                                       src=e.get("src", "none")))
                return self._json(dict(photos=photos))
            return super().do_GET()

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            store = load_store()
            if path == "/api/save":
                store["assets"][req["id"]] = dict(x=round(float(req["x"]), 3),
                                                  y=round(float(req["y"]), 3),
                                                  src="manual")
                save_store(store)
                stamp_collections(store)
                return self._json(dict(ok=True))
            if path == "/api/auto":
                ph = next((p for p in inventory() if p["id"] == req["id"]), None)
                if not ph:
                    return self._json(dict(ok=False), 404)
                session, base = immich_session()
                faces = fetch_faces(session, base, ph["id"]) if session else []
                fx, fy = compute_focus(os.path.join(ROOT, ph["grid"].lstrip("/")), faces)
                store["assets"][ph["id"]] = dict(x=fx, y=fy, src="auto")
                save_store(store)
                stamp_collections(store)
                return self._json(dict(ok=True, x=fx, y=fy))
            return self._json(dict(ok=False), 404)

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = "http://127.0.0.1:%d/" % port
        print("Focus editor on %s   (Ctrl-C to stop; every click saves)" % url)
        # webbrowser falls through to gio under WSL and complains to stderr;
        # opening a browser is a nicety, its failure isn't worth printing
        try:
            err = os.dup(2)
            with open(os.devnull, "w") as null:
                os.dup2(null.fileno(), 2)
            try:
                webbrowser.open(url)
            finally:
                os.dup2(err, 2)
                os.close(err)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true", help="compute focal points for photos that have none")
    ap.add_argument("--reseed", action="store_true", help="with --seed: also recompute existing auto entries")
    ap.add_argument("--stamp", action="store_true",
                    help="write the stored focal points into _data/collections.json")
    ap.add_argument("--edit", action="store_true", help="open the click-to-fix editor")
    ap.add_argument("--port", type=int, default=8777, help="editor port (default 8777)")
    ap.add_argument("--montage", metavar="OUT.png", help="QA contact sheet: centred crop vs focal crop")
    args = ap.parse_args()

    if args.montage:
        store = load_store()
        paths = [os.path.join(ROOT, p["grid"].lstrip("/")) for p in inventory()]
        print(montage([p for p in paths if os.path.exists(p)], args.montage))
    if args.seed:
        store = load_store()
        n = seed(store, reseed=args.reseed)
        save_store(store)
        print("%d focal point%s written to %s" % (n, "" if n == 1 else "s",
                                                  os.path.relpath(FOCUS_STORE, ROOT)))
        args.stamp = True
    if args.stamp:
        n = stamp_collections(load_store())
        print("%d photo%s updated in %s" % (n, "" if n == 1 else "s",
                                            os.path.relpath(COLLECTIONS, ROOT)))
    if args.edit:
        serve_editor(args.port)
    if not (args.seed or args.stamp or args.edit or args.montage):
        ap.print_help()


if __name__ == "__main__":
    main()
