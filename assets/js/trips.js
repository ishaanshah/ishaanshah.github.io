/* Trips — collection page interactivity
   Leaflet map (OpenTopoMap) + client-side GPX parsing + custom elevation
   profile, with filter / hover cross-linking between photos, routes and the
   elevation trace. Data comes from the #collection-data JSON emitted by the layout.
   Requires Leaflet (global L) to be loaded first. */
(function () {
  "use strict";

  var dataEl = document.getElementById("collection-data");
  var mapEl = document.getElementById("map");
  if (!dataEl || !mapEl || typeof L === "undefined") return; // not a traced collection page

  var collection = JSON.parse(dataEl.textContent);
  var traced = (collection.outings || []).filter(function (o) { return o.gpx; });
  if (!traced.length) return;

  var groupsEl = document.getElementById("groups");
  var legendEl = document.getElementById("legend");
  var outingFilters = document.getElementById("outing-filters");
  var elevSvg = document.getElementById("elev");
  var elevLabel = document.getElementById("elev-label");
  var elevOverlay = document.getElementById("elev-overlay");
  var SVGNS = "http://www.w3.org/2000/svg";

  var state = { active: null, focus: null, outings: {} };

  /* ---------- map ---------- */
  var map = L.map(mapEl, { scrollWheelZoom: false, attributionControl: false });
  map.setView([46, 2], 5);                 // provisional view; fitBounds sets the real one once tracks load
  L.tileLayer("https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png", {
    maxZoom: 18, subdomains: "abc",
    attribution: "© OpenStreetMap contributors, CyclOSM"
  }).addTo(map);
  // dedicated pane so every white casing sits below every colour line (no bringToBack timing issues)
  map.createPane("casings");
  map.getPane("casings").style.zIndex = 350;   // between tiles (200) and overlay (400)
  // clicking the map background (not a route) clears the current selection
  map.on("click", function () {
    if (!state.active) return;
    state.active = null;
    syncFilterUI(); filterGroups(); applyFocus();
  });

  /* ---------- helpers ---------- */
  function haversine(a, b) {
    var R = 6371000, toRad = Math.PI / 180;
    var dLat = (b.lat - a.lat) * toRad, dLng = (b.lng - a.lng) * toRad;
    var la1 = a.lat * toRad, la2 = b.lat * toRad;
    var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(la1) * Math.cos(la2) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  function parseGPX(text) {
    var xml = new DOMParser().parseFromString(text, "application/xml");
    var nodes = xml.getElementsByTagName("trkpt");
    var pts = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i], ele = n.getElementsByTagName("ele")[0];
      pts.push({
        lat: parseFloat(n.getAttribute("lat")),
        lng: parseFloat(n.getAttribute("lon")),
        ele: ele ? parseFloat(ele.textContent) : 0
      });
    }
    return pts;
  }
  function nearestIdx(pts, lat, lng) {
    var best = 0, bd = Infinity;
    for (var i = 0; i < pts.length; i++) {
      var d = haversine(pts[i], { lat: lat, lng: lng });
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  /* ---------- load every traced outing, then wire up ---------- */
  var allLatLngs = [];
  Promise.all(traced.map(function (o) {
    return fetch(o.gpx).then(function (r) { return r.text(); }).then(function (txt) {
      var pts = parseGPX(txt);
      // cumulative distance for elevation x-axis
      var cum = [0];
      for (var i = 1; i < pts.length; i++) cum[i] = cum[i - 1] + haversine(pts[i - 1], pts[i]);
      var total = cum[pts.length - 1] || 1;

      var latlngs = pts.map(function (p) { return [p.lat, p.lng]; });
      allLatLngs = allLatLngs.concat(latlngs);

      // white casing in the lower pane; colour line on top in the default overlay pane
      var casing = L.polyline(latlngs, {
        pane: "casings", color: "#fff", weight: 8, opacity: .9,
        interactive: false, lineJoin: "round", lineCap: "round"
      }).addTo(map);
      var poly = L.polyline(latlngs, {
        color: o.color, weight: 5, opacity: 1, lineJoin: "round", lineCap: "round",
        bubblingMouseEvents: false          // so a route click doesn't also trigger the map's clear-selection click
      }).addTo(map);

      var endStyle = { radius: 4, color: o.color, weight: 2.5, fillColor: "#fff", fillOpacity: 1 };
      L.circleMarker(latlngs[0], endStyle).addTo(map);
      L.circleMarker(latlngs[latlngs.length - 1], endStyle).addTo(map);

      // photo pins at each photo's own EXIF coords; also record t along track
      var pins = [], photoTs = [];
      (o.photos || []).forEach(function (p, i) {
        if (p.lat == null || p.lng == null) { photoTs.push(null); return; }
        var idx = nearestIdx(pts, p.lat, p.lng);
        photoTs.push(cum[idx] / total);
        var marker = L.marker([p.lat, p.lng], {
          icon: L.divIcon({
            className: "", iconSize: [22, 22], iconAnchor: [11, 11],
            html: '<div class="trip-pin" style="background:' + o.color + '">' + (i + 1) + "</div>"
          })
        }).addTo(map);
        marker.on("mouseover", function () { hoverPhoto(o.id, i, true); });
        marker.on("mouseout", function () { hoverPhoto(o.id, i, false); });
        marker.on("click", function () { scrollToPhoto(o.id, i); });
        pins.push(marker);
      });

      poly.on("mouseover", function () { setFocus(o.id, true); });
      poly.on("mouseout", function () { setFocus(o.id, false); });
      poly.on("click", function () { toggleActive(o.id); });

      state.outings[o.id] = {
        o: o, poly: poly, casing: casing, pins: pins,
        pts: pts, cum: cum, total: total, photoTs: photoTs
      };
    }).catch(function (e) { console.warn("GPX load failed for", o.id, e); });
  })).then(function () {
    map.invalidateSize();
    if (allLatLngs.length) map.fitBounds(L.latLngBounds(allLatLngs), { padding: [24, 24] });
    buildLegend();
    buildFilters();
    wirePhotos();
    applyFocus();
  });

  /* ---------- filters / legend ---------- */
  function buildFilters() {
    if (!outingFilters) return;               // filter chips removed from the layout
    outingFilters.addEventListener("click", function (e) {
      var b = e.target.closest(".chip"); if (!b) return;
      state.active = b.dataset.oid || null;
      syncFilterUI();
      filterGroups();
      applyFocus();
    });
    // hovering an outing chip previews its route
    Array.prototype.forEach.call(outingFilters.querySelectorAll(".chip"), function (b) {
      if (!b.dataset.oid) return;
      b.addEventListener("mouseenter", function () { if (state.outings[b.dataset.oid]) setFocus(b.dataset.oid, true); });
      b.addEventListener("mouseleave", function () { if (state.outings[b.dataset.oid]) setFocus(b.dataset.oid, false); });
    });
  }
  function buildLegend() {
    if (!legendEl) return;
    Array.prototype.forEach.call(legendEl.querySelectorAll(".leg"), function (el) {
      var oid = el.dataset.oid;
      el.addEventListener("mouseenter", function () { setFocus(oid, true); });
      el.addEventListener("mouseleave", function () { setFocus(oid, false); });
      el.addEventListener("click", function () { toggleActive(oid); });
    });
  }
  function syncFilterUI() {
    if (!outingFilters) return;               // no filter chips to sync
    Array.prototype.forEach.call(outingFilters.children, function (c) {
      c.setAttribute("aria-pressed", (c.dataset.oid || "") === (state.active || "") ? "true" : "false");
    });
  }
  function filterGroups() {
    Array.prototype.forEach.call(groupsEl.querySelectorAll(".pgroup"), function (sec) {
      sec.style.display = (!state.active || sec.dataset.oid === state.active) ? "" : "none";
    });
  }
  function toggleActive(oid) {
    state.active = state.active === oid ? null : oid;
    syncFilterUI(); filterGroups(); applyFocus();
  }

  /* ---------- focus / dim ---------- */
  function setFocus(oid, on) { state.focus = on ? oid : null; applyFocus(); }
  function applyFocus() {
    var eff = state.focus || state.active;
    Object.keys(state.outings).forEach(function (oid) {
      var s = state.outings[oid], emph = !eff || eff === oid;
      s.poly.setStyle({ opacity: emph ? 1 : 0.18 });
      s.casing.setStyle({ opacity: emph ? 0.9 : 0.12 });
      s.pins.forEach(function (m) {
        var el = m.getElement && m.getElement();
        if (el) el.firstChild.classList.toggle("dim", !emph);
      });
      if (legendEl) {
        var leg = legendEl.querySelector('.leg[data-oid="' + oid + '"]');
        if (leg) leg.classList.toggle("active", eff === oid);
      }
    });
    // elevation profile lives inside the map and appears while a route is hovered or selected
    if (eff) { if (elevOverlay) elevOverlay.hidden = false; drawElev(eff); }
    else if (elevOverlay) { elevOverlay.hidden = true; }
  }

  /* ---------- elevation profile (custom SVG) ---------- */
  function drawElev(oid) {
    var s = state.outings[oid]; if (!s) return;
    var o = s.o, pts = s.pts, cum = s.cum, total = s.total;
    elevLabel.innerHTML = '<span class="line" style="background:' + o.color + '"></span>' +
      o.name + " — ↑" + o.ascent + " · ↓" + o.descent;

    var W = 640, H = 120, padB = 22, padT = 12, padX = 6;
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) { if (p.ele < lo) lo = p.ele; if (p.ele > hi) hi = p.ele; });
    var X = function (c) { return padX + (c / total) * (W - 2 * padX); };
    var Y = function (e) { return H - padB - ((e - lo) / (hi - lo || 1)) * (H - padB - padT); };

    while (elevSvg.firstChild) elevSvg.removeChild(elevSvg.firstChild);
    for (var g = 0; g <= 3; g++) {
      var y = padT + g * ((H - padB - padT) / 3);
      line(elevSvg, padX, y, W - padX, y, "#eef2f0", 1);
    }
    var grad = document.createElementNS(SVGNS, "linearGradient");
    grad.id = "eg"; grad.setAttribute("x1", "0"); grad.setAttribute("y1", "0");
    grad.setAttribute("x2", "0"); grad.setAttribute("y2", "1");
    grad.innerHTML = '<stop offset="0" stop-color="' + o.color + '" stop-opacity=".28"/>' +
                     '<stop offset="1" stop-color="' + o.color + '" stop-opacity=".02"/>';
    elevSvg.appendChild(grad);

    var area = "M" + X(0) + "," + (H - padB) + " ";
    var d = "M";
    pts.forEach(function (p, i) {
      var px = X(cum[i]).toFixed(1), py = Y(p.ele).toFixed(1);
      area += "L" + px + "," + py + " ";
      d += (i ? "L" : "") + px + "," + py + " ";
    });
    area += "L" + X(total) + "," + (H - padB) + " Z";
    path(elevSvg, area, "url(#eg)", null, 0);
    path(elevSvg, d, "none", o.color, 2);

    text(elevSvg, padX, H - 6, "0 km", "#8a9994", "start");
    text(elevSvg, W - padX, H - 6, o.distance, "#8a9994", "end");

    // photo dots along the profile
    (o.photos || []).forEach(function (p, i) {
      var t = s.photoTs[i]; if (t == null) return;
      var c = t * total; // cumulative distance of this photo along the track
      var ele = eleAtDist(pts, cum, c);
      var gx = X(c), gy = Y(ele);
      var grp = document.createElementNS(SVGNS, "g");
      grp.setAttribute("class", "elev-dot"); grp.setAttribute("data-i", i);
      grp.setAttribute("transform", "translate(" + gx.toFixed(1) + " " + gy.toFixed(1) + ")");
      grp.innerHTML =
        '<line class="stem" x1="0" y1="0" x2="0" y2="' + (H - padB - gy).toFixed(1) + '" stroke="' + o.color + '" stroke-width="1" stroke-dasharray="2 2"/>' +
        '<circle r="5" fill="#fff" stroke="' + o.color + '" stroke-width="2"/>' +
        '<text y="-9" text-anchor="middle" font-size="10" font-weight="700" fill="' + o.color + '">' + (i + 1) + "</text>";
      elevSvg.appendChild(grp);
    });
  }
  function eleAtDist(pts, cum, c) {
    for (var i = 1; i < cum.length; i++) {
      if (cum[i] >= c) {
        var f = (c - cum[i - 1]) / ((cum[i] - cum[i - 1]) || 1);
        return pts[i - 1].ele * (1 - f) + pts[i].ele * f;
      }
    }
    return pts[pts.length - 1].ele;
  }
  function line(svg, x1, y1, x2, y2, stroke, w) {
    var l = document.createElementNS(SVGNS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1); l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    l.setAttribute("stroke", stroke); l.setAttribute("stroke-width", w); svg.appendChild(l);
  }
  function path(svg, d, fill, stroke, w) {
    var p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", d); p.setAttribute("fill", fill);
    if (stroke) { p.setAttribute("stroke", stroke); p.setAttribute("stroke-width", w); }
    svg.appendChild(p);
  }
  function text(svg, x, y, str, fill, anchor) {
    var t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y); t.setAttribute("fill", fill);
    t.setAttribute("font-size", "10"); t.setAttribute("text-anchor", anchor);
    t.setAttribute("font-family", "Inconsolata, monospace"); t.textContent = str;
    svg.appendChild(t);
  }

  /* ---------- photo <-> map <-> elevation ---------- */
  function photoEl(oid, i) {
    return groupsEl.querySelector('.photo[data-oid="' + oid + '"][data-i="' + i + '"]');
  }
  function wirePhotos() {
    Array.prototype.forEach.call(groupsEl.querySelectorAll(".photo"), function (el) {
      var oid = el.dataset.oid, i = +el.dataset.i;
      if (!state.outings[oid]) return; // untraced outing: no cross-linking
      el.addEventListener("mouseenter", function () { hoverPhoto(oid, i, true); });
      el.addEventListener("mouseleave", function () { hoverPhoto(oid, i, false); });
    });
  }
  function hoverPhoto(oid, i, on) {
    var el = photoEl(oid, i); if (el) el.classList.toggle("active", on);
    var s = state.outings[oid]; if (!s) return;
    setFocus(oid, on);
    if (s.pins[i]) {
      var pel = s.pins[i].getElement && s.pins[i].getElement();
      if (pel) pel.firstChild.classList.toggle("active", on);
    }
    if (on) {
      var dot = elevSvg.querySelector('.elev-dot[data-i="' + i + '"]');
      if (dot) dot.classList.add("active");
    }
  }
  function scrollToPhoto(oid, i) {
    var el = photoEl(oid, i);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
})();
