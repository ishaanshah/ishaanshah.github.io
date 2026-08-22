/* Justified photo rows — the Immich timeline layout.
   Every photo keeps its own aspect ratio (nothing is cropped); each row is
   scaled to exactly fill the container at a shared height near --row-h, which
   itself scales with the viewport so the same code covers phone → desktop.
   The CSS flex rules already give an approximate justification without JS;
   this refines the row breaks so a nearly-empty row can't balloon. */
(function () {
  "use strict";

  var boxes = Array.prototype.slice.call(document.querySelectorAll(".trips .photos"));
  if (!boxes.length) return;

  // below this container width a row holds only one or two photos, so the
  // trailing row is justified rather than left short
  var NARROW = 500;

  function ratio(el) {
    var ar = parseFloat(el.style.getPropertyValue("--ar"));
    return ar > 0 ? ar : 1.5;
  }

  // width of each item, rounded so the pixels add up to exactly the row width
  function place(row, h) {
    var acc = 0, prev = 0;
    row.forEach(function (el) {
      acc += ratio(el) * h;
      var w = Math.round(acc) - prev;
      prev = Math.round(acc);
      el.style.flex = "0 0 auto";
      el.style.aspectRatio = "auto";
      el.style.width = w + "px";
      el.style.height = Math.round(h) + "px";
    });
  }

  // height at which `sum` of aspect ratios plus the gaps spans exactly width w
  function fit(w, gap, n, sum) { return (w - gap * (n - 1)) / sum; }

  function metrics(box) {
    var cs = getComputedStyle(box);
    return {
      w: box.clientWidth,
      gap: parseFloat(cs.columnGap) || 0,
      // --row-h is a clamp() against the viewport, so it moves with the screen
      target: parseFloat(cs.getPropertyValue("--row-h")) || 215
    };
  }

  function layout(box, m) {
    var items = Array.prototype.slice.call(box.querySelectorAll(".photo"));
    var w = m.w, gap = m.gap, target = m.target;
    if (!items.length || !w) return;             // hidden group: laid out again when shown

    var row = [], sum = 0;
    items.forEach(function (el) {
      var ar = ratio(el);
      row.push(el); sum += ar;
      var h = fit(w, gap, row.length, sum);
      if (h >= target) return;                   // row still too tall: keep filling it
      // adding this photo overshot; keep whichever row lands nearer the target
      var without = row.length > 1 ? fit(w, gap, row.length - 1, sum - ar) : Infinity;
      if (Math.abs(without - target) < Math.abs(h - target)) {
        row.pop();
        place(row, without);
        row = [el]; sum = ar;
        // a lone very wide photo can already be shorter than the target
        if (w / sum < target) { place(row, w / sum); row = []; sum = 0; }
      } else {
        place(row, h);
        row = []; sum = 0;
      }
    });
    if (row.length) {
      // Trailing row. On a wide container it stays at the target height, so a
      // lone photo doesn't balloon across the grid. On a narrow one — a phone,
      // where a row holds one or two photos — every other row is essentially
      // full width, so a short last row reads as a broken grid: stretch it to
      // fill, as long as that isn't a wild blow-up (a lone portrait).
      // `Math.min` also caps the height at what fits, so a wide photo can
      // never push the row past the container.
      var full = fit(w, gap, row.length, sum);
      var stretch = w < NARROW && full <= target * 1.8;
      place(row, stretch ? full : Math.min(target, full));
    }
  }

  boxes.forEach(function (box) {
    // only the container's width and the target height matter, and laying out
    // changes the box's height — gating on those keeps the observer from
    // re-entering its own resize
    var last = "";
    function run() {
      var m = metrics(box);
      var key = m.w + "/" + m.gap + "/" + m.target;
      if (key === last) return;
      last = key;
      layout(box, m);
    }
    run();
    if (window.ResizeObserver) new ResizeObserver(run).observe(box);
    // --row-h is viewport-relative, so it can move without the box resizing
    // (rotation while the container width is capped): watch the window too
    window.addEventListener("resize", run);
    window.addEventListener("orientationchange", run);
  });
})();
