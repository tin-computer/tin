// What a film draws, in parts. The dashboard runs in its own window, an iframe exactly the
// screen's size, so it lays out, positions its popovers and answers its breakpoints as in a
// browser. `installScreen` runs inside it: the marks on what changed, among the app's own
// layers, and the measures the recorder takes. `installStage` runs on the page around it, the
// stage: the backdrop, the fade, the cursor and a click's ring, designed in output pixels and
// scaled onto the part of the stage in frame. The caption band is drawn apart, on a strip under
// everything the camera frames, at the output's own pixels, so it holds still whatever the camera
// does. The
// recorder sets every value each frame; nothing here animates by itself. Both functions are
// injected as init scripts, so each must stay self-contained.

export function installScreen() {
  if (window === window.top) return;
  const film = window.__film = window.__film || {};

  // A film shows no scrollbars.
  const quiet = document.createElement("style");
  quiet.textContent = "* { scrollbar-width: none !important; }";
  // Init scripts can run before the document has its <html>.
  const place = () => document.documentElement && (document.documentElement.append(quiet), true);
  if (!place()) new MutationObserver((_, observer) => place() && observer.disconnect()).observe(document, {childList: true});

  // Requests in flight. The recorder waits for none before it shoots a frame, so a response
  // always lands in the frame that asked for it. Bodies are read before the promise resolves.
  film.pending = 0;
  const realFetch = window.fetch.bind(window);
  window.fetch = (...args) => {
    film.pending++;
    return realFetch(...args)
      .then(async response => {
        await response.clone().arrayBuffer().catch(() => null);
        return response;
      })
      .finally(() => film.pending--);
  };

  const ours = "[data-film-layer]";
  // Marks live among the app's own layers: under its rail and toasts, or (`over`) above its panel,
  // and inside whatever fades.
  let marks = null;
  function mount() {
    if (marks?.isConnected) return;
    marks = document.createElement("div");
    marks.setAttribute("data-film-layer", "");
    marks.innerHTML = `<style>
      [data-film-marks] { position: fixed; left: 0; top: 0; width: 0; height: 0; pointer-events: none; }
      [data-film-marks="under"] { z-index: 9; }
      [data-film-marks="over"] { z-index: 61; }
      [data-film-marks] > i { position: absolute; border-radius: 5px; background: rgb(var(--accent-deep-rgb, 153 175 98) / 16%); box-shadow: inset 0 0 0 1.5px rgb(var(--accent-deep-rgb, 153 175 98) / 55%); }
    </style><div data-film-marks="under"></div><div data-film-marks="over"></div>`;
    document.body.append(marks);
  }

  // One frame's work inside the screen: draw the marks (boxes in the screen's px) and say which
  // cursor the page would show at the hand's point: the hand over a control it points at (a link,
  // a button, a box), as Stageshow does; the arrow over everything else, a clickable row included.
  film.frame = ({highlights = [], cursor = null}) => {
    mount();
    for (const layer of ["under", "over"]) {
      marks.querySelector(`[data-film-marks="${layer}"]`).innerHTML = highlights.filter(mark => (mark.over ? "over" : "under") === layer)
        .map(mark => `<i style="left:${mark.x}px;top:${mark.y}px;width:${mark.width}px;height:${mark.height}px;opacity:${mark.opacity}"></i>`).join("");
    }
    if (!cursor) return "pointer";
    const under = document.elementsFromPoint(cursor.x, cursor.y).find(element => !element.closest(ours));
    const control = under?.closest("a[href], button, [role='button'], [role='tab'], input, label, select, summary");
    return control && getComputedStyle(under).cursor === "pointer" ? "hand" : "pointer";
  };

  // Whether what is on top at a point is, or is inside, a match.
  film.hits = (selectors, x, y) => {
    const top = document.elementsFromPoint(x, y).find(element => !element.closest(ours));
    return Boolean(top?.closest([].concat(selectors).join(", ")));
  };

  // Every word and control the page draws, as boxes: each line of text, each field and button.
  // What a panel covers or a container clips is left out: the hit at its middle must land on it.
  const pieces = () => {
    const probe = document.createElement("style");
    probe.textContent = "* { pointer-events: auto !important; }";
    document.documentElement.append(probe);
    const shown = (element, box) => {
      const style = getComputedStyle(element);
      if (style.visibility === "hidden" || Number(style.opacity) === 0) return false;
      const top = document.elementsFromPoint(box.left + box.width / 2, box.top + box.height / 2).find(item => !item.closest(ours));
      return Boolean(top && element.contains(top));
    };
    const found = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent.trim();
      const element = node.parentElement;
      if (!text || !element || element.closest(ours)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const box of range.getClientRects()) {
        if (box.width && box.height && shown(element, box)) found.push({text: text.slice(0, 40), box, size: parseFloat(getComputedStyle(element).fontSize)});
      }
    }
    for (const element of document.body.querySelectorAll("input, button, select, textarea, [role=tab]")) {
      const box = element.getBoundingClientRect();
      if (box.width && box.height && shown(element, box)) found.push({text: element.getAttribute("aria-label") || element.textContent.trim().slice(0, 40) || element.tagName.toLowerCase(), box});
    }
    probe.remove();
    return found;
  };
  const crosses = (a, b, at) => a < at - 0.5 && b > at + 0.5;
  // Cut by the top or bottom edge, or by a side.
  const byRows = (box, f) => box.right > f.x + 0.5 && box.left < f.x + f.width - 0.5 && (crosses(box.top, box.bottom, f.y) || crosses(box.top, box.bottom, f.y + f.height));
  const bySides = (box, f) => box.bottom > f.y + 0.5 && box.top < f.y + f.height - 0.5 && (crosses(box.left, box.right, f.x) || crosses(box.left, box.right, f.x + f.width));
  // What the picture's edges cut through.
  film.sliced = picture => [...new Set(pieces().filter(({box}) => byRows(box, picture) || bySides(box, picture)).map(item => item.text))];
  // The smallest move, up to `most` px each way, that keeps the frame in the room and leaves every
  // word and control whole or wholly out; none if there is no such line.
  film.clean = (frame, pictureHeight, room, most) => {
    const found = pieces();
    const steps = [0];
    for (let d = 1; d <= most; d++) steps.push(d, -d);
    const at = (dx, dy) => ({x: frame.x + dx, y: frame.y + dy, width: frame.width, height: pictureHeight});
    const fits = (dx, dy) => frame.x + dx >= room.x - 0.5 && frame.x + dx + frame.width <= room.x + room.width + 0.5
      && frame.y + dy >= room.y - 0.5 && frame.y + dy + pictureHeight <= room.y + room.height + 1;
    const dy = steps.find(d => fits(0, d) && !found.some(({box}) => byRows(box, at(0, d)))) ?? 0;
    const dx = steps.find(d => fits(d, dy) && !found.some(({box}) => bySides(box, at(d, dy)))) ?? 0;
    return {dx, dy};
  };
  // The text a frame shows, with its size: everything drawn and wholly inside it.
  film.textSizes = frame => pieces().filter(({box, size}) => size && box.left >= frame.x - 1 && box.top >= frame.y - 1
    && box.right <= frame.x + frame.width + 1 && box.bottom <= frame.y + frame.height + 1).map(({text, size}) => ({text, size}));

  // Where something is: the union of every match, in the screen's px. With `text`, only what is
  // written inside them, so a full-width row frames as its words.
  const textBoxes = element => {
    const boxes = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      boxes.push(...range.getClientRects());
    }
    // Controls with no words (a switch, a box) still count.
    for (const control of element.querySelectorAll("input, button, [role=switch]")) boxes.push(control.getBoundingClientRect());
    return boxes;
  };
  film.rect = (selectors, text = false) => {
    const boxes = [].concat(selectors).flatMap(selector => [...document.querySelectorAll(selector)])
      .flatMap(element => (text ? textBoxes(element) : [element.getBoundingClientRect()])).filter(box => box.width && box.height);
    if (!boxes.length) return null;
    const x = Math.min(...boxes.map(box => box.left));
    const y = Math.min(...boxes.map(box => box.top));
    return {x, y, width: Math.max(...boxes.map(box => box.right)) - x, height: Math.max(...boxes.map(box => box.bottom)) - y};
  };
}

export function installStage({backdrop = "#000", path = "/", strip = null, size = null} = {}) {
  if (window !== window.top || location.pathname !== path) return;
  const film = window.__film = window.__film || {};

  // The pointer and the pointing hand, after Stageshow's MacPointer and MacPointingHand (MIT): the
  // arrow's hotspot at its tip, the hand's at its fingertip.
  const POINTER = '<svg viewBox="10 7 13 19" width="100%" height="100%" fill="none"><g transform="translate(10 7)"><path d="m6.148 18.473 1.863-1.003 1.615-.839-2.568-4.816h4.332l-11.379-11.408v16.015l3.316-3.221z" fill="#fff"/><path d="m6.431 17 1.765-.941-2.775-5.202h3.604l-8.025-8.043v11.188l2.53-2.442z" fill="#000"/></g></svg>';
  const HAND = '<svg viewBox="0 0 32 32" width="100%" height="100%" fill="none"><path d="M10.6 6a1.7 1.7 0 0 1 3.4 0v6.6a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0V19c0 3.1-2 5.6-5 5.6h-2.6c-2 0-3.3-.9-4.4-2.4l-3.1-4.1c-.6-.9-.4-2 .5-2.5.8-.4 1.7-.2 2.3.5l1.1 1.4V6Z" fill="#fff" stroke="#000" stroke-width="1.1" stroke-linejoin="round"/><path d="M14 12.6v3.6M16.6 13.3v3M19.2 14v2.6" stroke="#000" stroke-width="1" stroke-linecap="round"/></svg>';

  let root = null;
  // Frames drawn, and the cursor's kind: which glyph shows, which it came from and when.
  let frames = 0;
  const glyph = {kind: "pointer", from: null, at: -100};
  const parts = {};
  function mount() {
    if (root?.isConnected) return;
    parts.strip?.remove();
    root = document.createElement("div");
    root.setAttribute("data-film-overlay", "");
    root.innerHTML = `<style>
      [data-film-overlay] { position: fixed; z-index: 2147483647; pointer-events: none; overflow: hidden; contain: strict; }
      [data-film-overlay] * { box-sizing: border-box; }
      [data-film-stage] { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
      [data-film-fade], [data-film-band] { position: absolute; }
      /* Under the picture: it takes the pointer while the hand travels, so nothing in the screen
         is hovered. The band itself is drawn apart, at the output's own pixels. */
      [data-film-band] { pointer-events: auto; }
      [data-film-cursor] { position: absolute; left: 0; top: 0; }
      [data-film-ripples] > i { position: absolute; border-radius: 50%; }
    </style><div data-film-stage>
      <div data-film-fade></div>
      <div data-film-band></div>
      <div data-film-ripples></div>
      <div data-film-cursor="pointer">${POINTER}</div>
      <div data-film-cursor="hand">${HAND}</div>
    </div>`;
    document.body.append(root);
    // The caption band, drawn apart at one device pixel per output pixel: the backdrop and one
    // caption, centred, in the app's sans.
    if (strip && size) {
      const px = strip.width / size.width;
      const unit = Math.min(size.width, size.height) * px;
      parts.strip = document.createElement("div");
      parts.strip.setAttribute("data-film-strip", "");
      parts.strip.innerHTML = `<style>
        [data-film-strip] { position: fixed; left: ${strip.x}px; top: ${strip.y}px; width: ${strip.width}px; height: ${strip.height}px; background: ${backdrop}; display: grid; place-items: center; padding: 0 ${unit * 0.08}px; box-sizing: border-box; pointer-events: none; }
        [data-film-strip] p { margin: 0; text-align: center; text-wrap: balance; font-family: var(--sans, sans-serif); font-weight: 400; font-feature-settings: "kern";
          font-size: ${Math.round(Math.min(size.width, size.height) * 0.037) * px}px; line-height: ${Math.round(Math.min(size.width, size.height) * 0.046) * px}px; letter-spacing: -0.01em; }
      </style><p></p>`;
      parts.unit = unit;
      document.body.append(parts.strip);
    }
    for (const name of ["stage", "fade", "band", "ripples"]) parts[name] = root.querySelector(`[data-film-${name}]`);
    parts.pointer = root.querySelector('[data-film-cursor="pointer"]');
    parts.hand = root.querySelector('[data-film-cursor="hand"]');
  }

  // state: {frame: {x, y, width, height} in the stage's px, size: {width, height} and band in
  // output px, caption: {text, opacity, rise}, cursor: {x, y, visible, kind, press, opacity},
  // ripples: [{x, y, progress, fading}], fade (0 to 1)}.
  film.render = state => {
    mount();
    const {frame, size} = state;
    const k = size.width / frame.width;
    // A few px past the frame on every side: the shot is clipped to half pixels, and close in, the
    // screen must not peek out under the band or past the edge.
    const spill = 4;
    Object.assign(root.style, {left: `${frame.x - spill}px`, top: `${frame.y - spill}px`, width: `${frame.width + spill * 2}px`, height: `${frame.height + spill * 2}px`});
    Object.assign(parts.stage.style, {left: `${spill}px`, top: `${spill}px`, width: `${size.width}px`, height: `${size.height}px`, transform: `scale(${1 / k})`});
    const unit = Math.min(size.width, size.height);
    const out = spill * k;

    // The band, the fades and the margin are all the backdrop.
    Object.assign(parts.fade.style, {inset: `${-out}px`, background: backdrop, opacity: String(state.fade || 0)});
    const band = state.band || 0;
    Object.assign(parts.band.style, {left: `${-out}px`, right: `${-out}px`, bottom: `${-out}px`, height: `${band + out}px`, display: band ? "block" : "none"});
    if (parts.strip) {
      const caption = state.caption || {};
      const words = parts.strip.querySelector("p");
      words.textContent = caption.text || "";
      Object.assign(words.style, {color: getComputedStyle(document.body).color, opacity: String(caption.opacity ?? 0), transform: `translateY(${(caption.rise || 0) * parts.unit * 0.02}px)`});
    }

    // The cursor is drawn at macOS's size in the screen's own px, Stageshow's large style (1.5x),
    // so it grows with the camera as if filmed with the screen. A change of kind crosses over in
    // 0.2 s: the new glyph grows from its hotspot as the old one shrinks into it.
    const cursor = state.cursor || {};
    frames++;
    const kind = cursor.kind || "pointer";
    if (kind !== glyph.kind) Object.assign(glyph, {kind, from: glyph.kind, at: frames});
    const swap = Math.min(1, (frames - glyph.at) / 6);
    const grown = swap * swap * (3 - 2 * swap);
    const at = `${(cursor.x - frame.x) * k}px, ${(cursor.y - frame.y) * k}px`;
    // In screen px: the arrow 13 by 19 units at 0.75 px a unit, the hand 32 units at 0.8125.
    const LARGE = 1.5;
    const sizes = {pointer: {unit: 0.75 * LARGE * k, width: 13, height: 19, hot: [0, 0]}, hand: {unit: 0.8125 * LARGE * k, width: 32, height: 32, hot: [12, 4]}};
    for (const name of ["pointer", "hand"]) {
      const {unit: u, width, height, hot} = sizes[name];
      const share = name === glyph.kind ? grown : name === glyph.from ? 1 - grown : 0;
      Object.assign(parts[name].style, {
        width: `${width * u}px`, height: `${height * u}px`, display: cursor.visible && share > 0 ? "block" : "none",
        opacity: String((cursor.opacity ?? 1) * share), transformOrigin: `${hot[0] * u}px ${hot[1] * u}px`,
        transform: `translate(${at}) translate(${-hot[0] * u}px, ${-hot[1] * u}px) scale(${share * (cursor.press ?? 1)})`,
        filter: `drop-shadow(0 ${1 * LARGE * k}px ${2 * LARGE * k}px rgba(0, 0, 0, 0.3))`,
      });
    }

    // Stageshow's ring: 36 screen px, a 1.5 px edge in the accent over a tenth of it, from a fifth
    // of its size to all of it as it fades.
    parts.ripples.innerHTML = (state.ripples || []).map(ripple => {
      const size = 36 * k * (0.2 + 0.8 * ripple.progress);
      return `<i style="left:${(ripple.x - frame.x) * k - size / 2}px;top:${(ripple.y - frame.y) * k - size / 2}px;width:${size}px;height:${size}px;border:${1.5 * k}px solid rgb(var(--accent-deep-rgb, 153 175 98));background:rgb(var(--accent-deep-rgb, 153 175 98) / 10%);opacity:${(1 - ripple.progress) * (ripple.fading ?? 1)}"></i>`;
    }).join("");
  };
}
