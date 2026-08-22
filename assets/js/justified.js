/* Justified photo rows — the Immich timeline layout.
   Every photo keeps its own aspect ratio (nothing is cropped); each row is
   scaled to exactly fill the container at a shared height near --row-h. A
   trailing partial row is left at the target height rather than stretched.
   The CSS flex rules already give an approximate justification without JS;
   this refines the row breaks so a nearly-empty row can't balloon. */
(function () {
  "use strict";

  var boxes = Array.prototype.slice.call(document.querySelectorAll(".trips .photos"));
  if (!boxes.length) return;

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

  function layout(box) {
    var items = Array.prototype.slice.call(box.querySelectorAll(".photo"));
    var w = box.clientWidth;
    if (!items.length || !w) return;             // hidden group: laid out again when shown

    var cs = getComputedStyle(box);
    var gap = parseFloat(cs.columnGap) || 0;
    var target = parseFloat(cs.getPropertyValue("--row-h")) || 215;

    var row = [], sum = 0;
    items.forEach(function (el) {
      var ar = ratio(el);
      row.push(el); sum += ar;
      var h = (w - gap * (row.length - 1)) / sum;
      if (h >= target) return;                   // row still too tall: keep filling it
      // adding this photo overshot; keep whichever row lands nearer the target
      var without = row.length > 1 ? (w - gap * (row.length - 2)) / (sum - ar) : Infinity;
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
    // trailing row never fills the width, so hold it at the target height
    if (row.length) place(row, target);
  }

  boxes.forEach(function (box) {
    // only the container's width matters, and laying out changes its height —
    // gating on width keeps the observer from re-entering its own resize
    var lastW = -1;
    function run() {
      var w = box.clientWidth;
      if (w === lastW) return;
      lastW = w;
      layout(box);
    }
    run();
    if (window.ResizeObserver) new ResizeObserver(run).observe(box);
    else window.addEventListener("resize", run);
  });
})();
