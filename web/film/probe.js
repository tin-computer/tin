// What mographc measures on each frame: every element on the stage and inside each window's page,
// with its box, the part of it its containers show, its opacity, its stretch, its text and its
// font, in the picture's px (web/film/vendor/mographc reads these rows). Adapted from mographc's
// probe (src/probe/Probe.tsx, MIT, Copyright (c) 2026 Replicas): a film here is a live page, not
// a Remotion composition, so the probe walks each document, open shadow roots included, and the
// recorder places each window's rows on the stage through its iframe.

// Runs inside a document. Returns its rows in the document's own viewport px, and the iframes in
// it with where they show, so the recorder can measure what's inside them.
export function probeDocument({cursor = null} = {}) {
  const r1 = n => Math.round(n * 10) / 10;
  const intersect = (a, b) => {
    const x = Math.max(a[0], b[0]);
    const y = Math.max(a[1], b[1]);
    return [x, y, Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - x), Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - y)];
  };
  const multiply = (p, q) => [p[0] * q[0] + p[2] * q[1], p[1] * q[0] + p[3] * q[1], p[0] * q[2] + p[2] * q[3], p[1] * q[2] + p[3] * q[3]];
  const ownLinear = style => {
    let m = [1, 0, 0, 1];
    const t = style.transform.match(/^matrix\(([^)]+)\)/);
    if (t) m = t[1].split(",").slice(0, 4).map(Number);
    const rotate = style.rotate.match(/^(-?[\d.]+)deg$/);
    if (rotate) {
      const r = (Number(rotate[1]) * Math.PI) / 180;
      m = multiply(m, [Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r)]);
    }
    if (style.scale && style.scale !== "none") {
      const [sx, sy = sx] = style.scale.split(" ").map(Number);
      m = multiply(m, [sx, 0, 0, sy]);
    }
    return m;
  };
  const stretchOf = ([a, b, c, d]) => {
    const s = a * a + b * b + c * c + d * d;
    const det = Math.abs(a * d - b * c);
    const disc = Math.sqrt(Math.max(s * s - 4 * det * det, 0));
    return Math.sqrt(Math.sqrt((s + disc) / 2) / Math.sqrt(Math.max((s - disc) / 2, 1e-9)));
  };
  // An element's place in its page by tag alone. mographc's probe also hashes the class, which
  // holds still in a Remotion film, but a product toggles state classes (is-open, is-visible) in
  // the middle of a transition, and the same card would read as one leaving and another arriving.
  const signature = el => el.tagName.toLowerCase();
  const fonts = new Map();
  document.fonts.forEach(face => {
    const family = face.family.replace(/["']/g, "");
    fonts.set(family, (fonts.get(family) ?? false) || face.status === "loaded");
  });

  const view = [0, 0, innerWidth, innerHeight];
  const unbounded = [-1e6, -1e6, 2e6, 2e6];
  const rows = [];
  const frames = [];
  const root = document.documentElement;
  const state = new Map([[root, {opacity: 1, clip: unbounded, linear: [1, 0, 0, 1], path: "r"}]]);
  // Elements in document order, through open shadow roots; each with the element whose box,
  // opacity and transforms it inherits.
  const walk = function* (node, parent) {
    for (const el of node.children) {
      yield [el, parent];
      if (el.shadowRoot) yield* walk(el.shadowRoot, el);
      // An SVG is one drawing, except a diagram's ([data-diagram]): its marks move on their own.
      if (el.tagName !== "IFRAME" && (el.tagName !== "svg" || el.closest("[data-diagram]"))) yield* walk(el, el);
    }
  };
  for (const [el, parent] of walk(root, root)) {
    const up = state.get(parent) ?? state.get(root);
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const sig = signature(el);
    let n = 0;
    for (const sibling of (el.parentNode ?? parent).children) {
      if (sibling === el) break;
      if (signature(sibling) === sig) n += 1;
    }
    const path = `${up.path}/${sig}#${n}`;
    const hidden = style.visibility === "hidden" || style.display === "none";
    const opacity = up.opacity * Number(style.opacity) * (hidden ? 0 : 1);
    const linear = multiply(up.linear, ownLinear(style));
    let clip = up.clip;
    if (style.overflow !== "visible" && el.tagName !== "svg") clip = intersect(clip, [rect.left, rect.top, rect.width, rect.height]);
    state.set(el, {opacity, clip, linear, path});
    // The film's own cursor glyphs are measured once, below, as the one cursor they draw.
    if (el.closest("[data-film-cursor]")) continue;
    if (el.tagName === "IFRAME") {
      if (opacity >= 0.01 && rect.width * rect.height > 0) {
        frames.push({id: el.id, k: path, b: [rect.left, rect.top, rect.width, rect.height], v: intersect(clip, [rect.left, rect.top, rect.width, rect.height]), o: opacity, sx: rect.width / (el.clientWidth || rect.width), sy: rect.height / (el.clientHeight || rect.height)});
      }
      continue;
    }
    const tag = el.getAttribute("data-motion");
    // The document itself is the canvas: it grows and shrinks with whatever a route shows.
    if (el === document.body) continue;
    const mark = el.closest("svg") && el.tagName !== "svg";
    if (mark && !el.closest("[data-diagram]")) continue;
    // A straight stroked line has no area of its own; its ink is its stroke.
    const stroked = mark && style.stroke !== "none" && parseFloat(style.strokeWidth) > 0;
    if (opacity < 0.01 || (rect.width * rect.height < 4 && !tag && !stroked)) continue;
    const text = Array.from(el.childNodes).filter(node => node.nodeType === 3).map(node => node.textContent ?? "").join("").trim();
    const shadows = [style.boxShadow, style.textShadow, ...(style.filter.match(/drop-shadow\([^)]*\)[^)]*\)?/g) ?? [])].filter(x => x && x !== "none");
    const inked = colour => colour && colour !== "none" && !/rgba\(0, 0, 0, 0\)|transparent/.test(colour);
    const paints = mark
      ? inked(style.fill) || (inked(style.stroke) && parseFloat(style.strokeWidth) > 0)
      : !/rgba\(0, 0, 0, 0\)|transparent/.test(style.backgroundColor) || style.borderTopWidth !== "0px" || shadows.length > 0 || el.tagName === "svg" || el.tagName === "IMG";
    if (!text && !paints && !tag) continue;
    let measured = [rect.left, rect.top, rect.width, rect.height];
    // A stroked mark's box is its line's; its ink reaches half the stroke past it (a Sankey's
    // flow is a line as wide as the people it carries).
    if (mark && inked(style.stroke) && parseFloat(style.strokeWidth) > 0) {
      const m = el.getScreenCTM?.();
      const half = (parseFloat(style.strokeWidth) / 2) * (m ? Math.hypot(m.a, m.b) : 1);
      // Across its run: a wide flow's ink spreads up and down, a tall line's left and right.
      const [x, y] = rect.width >= rect.height ? [0, half] : [half, 0];
      measured = [rect.left - x, rect.top - y, rect.width + 2 * x, rect.height + 2 * y];
    }
    const node = Array.from(el.childNodes).find(child => child.nodeType === 3 && child.textContent.trim());
    if (text && node) {
      const range = document.createRange();
      range.selectNodeContents(node);
      const g = range.getBoundingClientRect();
      if (g.width * g.height > 0) measured = [g.left, g.top, g.width, g.height];
    }
    const visible = intersect(clip, measured);
    // Only what the picture can show: a page's rows scrolled far out of view are left out.
    if (!tag && intersect(visible, view)[2] * intersect(visible, view)[3] < 1) continue;
    const scale = el.offsetWidth ? rect.width / el.offsetWidth : 1;
    const family = style.fontFamily.split(",")[0].replace(/["']/g, "").trim();
    const row = {k: path, t: text ? "t" : el.tagName === "svg" || el.tagName === "IMG" ? "g" : "b", b: measured.map(r1), v: visible.map(r1), o: Math.round(opacity * 1000) / 1000};
    if (text) {
      row.x = text.slice(0, 80);
      row.fs = r1(parseFloat(style.fontSize) * scale);
      row.c = style.color;
      row.fw = Number(style.fontWeight) || 400;
      row.ff = family;
      if (fonts.has(family) && !fonts.get(family)) row.fb = family;
    }
    if (paints && !text) row.bg = mark ? (inked(style.fill) ? style.fill : style.stroke) : style.backgroundColor;
    if (el.tagName === "svg") {
      const first = el.querySelector("path");
      row.fill = first?.getAttribute("fill") ?? "";
      if (first) {
        const ps = getComputedStyle(first);
        row.pf = ps.fill;
        if (ps.stroke !== "none" && parseFloat(ps.strokeWidth) > 0) row.ps = ps.stroke;
      }
      row.d = first?.getAttribute("d")?.slice(0, 120) ?? "";
    }
    if (el.tagName === "IMG" && el.complete && !el.naturalWidth) row.br = 1;
    if (shadows.length) row.sh = shadows.join(" | ").slice(0, 200);
    const stretch = stretchOf(linear);
    if (stretch > 1.001) row.st = Math.round(stretch * 1000) / 1000;
    if (tag) row.id = tag;
    // A diagram's marks are many of one shape that move at once; one with an id keeps it.
    else if (mark && el.id) row.id = `surface:${el.id}`;
    const ignore = el.getAttribute("data-motion-ignore");
    if (ignore) row.ig = ignore;
    // A diagram's flows share boxes they never both cover: seeing through each other is the picture.
    else if (mark) row.ig = "staging/double-exposure: a diagram's marks overlap only in their boxes";
    // The film's own marks, drawn in the page: part of the picture, not of the product.
    if (el.closest("[data-film-layer]")) row.film = 1;
    rows.push(row);
  }
  // The cursor: whichever of its glyphs shows most, with its tip where the hand points.
  if (cursor) {
    const glyph = [...document.querySelectorAll("[data-film-cursor]")]
      .map(el => [el, Number(getComputedStyle(el).opacity), getComputedStyle(el).display])
      .filter(([, opacity, display]) => display !== "none" && opacity >= 0.01)
      .sort((a, b) => b[1] - a[1])[0];
    if (glyph) {
      const rect = glyph[0].getBoundingClientRect();
      const box = [rect.left, rect.top, rect.width, rect.height].map(r1);
      rows.push({k: "r/cursor", t: "g", b: box, v: box, o: Math.round(glyph[1] * 1000) / 1000, id: "cursor:hand", tp: [r1(cursor.x), r1(cursor.y)], pf: "rgb(0, 0, 0)", ps: "rgb(255, 255, 255)"});
    }
  }
  // On the stage, the picture ends where the caption band starts: nothing under it is seen.
  const band = document.querySelector("[data-film-band]");
  const picture = band ? band.getBoundingClientRect().top : innerHeight;
  return {rows, frames, size: [innerWidth, innerHeight], picture};
}

// One frame, measured twice. `pages` holds each window's own page in its own px, where only the
// product moves things, so a pop, a flicker or a jump there is the product's. `picture` is what
// the viewer sees: the stage, with each window's rows placed where its iframe shows them, cut by
// the frame and the caption band. A row placed on the picture keeps only what its page shows of
// it, so text the product itself truncates isn't counted as cut by the frame. `windows` maps an
// iframe's id to its Playwright frame; only the `products` among them are checked as product
// pages (a desk's terminal is the film's own drawing).
export async function measureFrame(page, windows, {f, cursor, products = Object.keys(windows)}) {
  const stage = await page.evaluate(probeDocument, {cursor});
  const r = n => Math.round(n * 10) / 10;
  const clip = (a, b) => {
    const left = Math.max(a[0], b[0]);
    const top = Math.max(a[1], b[1]);
    return [left, top, Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - left), Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - top)];
  };
  const shown = [0, 0, stage.size[0], stage.picture];
  const rows = [...stage.rows];
  const pages = {};
  for (const frame of stage.frames) {
    const inside = windows[frame.id];
    const seen = clip(frame.v, shown);
    if (!inside || seen[2] * seen[3] < 1) continue;
    const own = await inside.evaluate(probeDocument, {});
    if (products.includes(frame.id)) pages[frame.id] = {f, c: own.size, e: own.rows.filter(row => !row.film)};
    const [x, y] = frame.b;
    const place = box => [x + box[0] * frame.sx, y + box[1] * frame.sy, box[2] * frame.sx, box[3] * frame.sy];
    for (const row of own.rows) {
      // What the page and its window show of it is its box; the picture's edge alone can cut it.
      const box = clip(place(row.v), frame.v).map(r);
      if (box[2] * box[3] < 1 && !row.id) continue;
      const placed = {...row, k: `${frame.k}/${frame.id}:${row.k}`, b: box, v: clip(box, seen).map(r), o: Math.round(row.o * frame.o * 1000) / 1000};
      if (row.fs) placed.fs = r(row.fs * frame.sy);
      rows.push(placed);
    }
  }
  return {picture: {f, c: stage.size, e: rows}, pages};
}
