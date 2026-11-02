// What a film draws, in parts. The dashboard runs in its own window, an iframe exactly the
// screen's size, so it lays out, positions its popovers and answers its breakpoints as in a
// browser. `installScreen` runs inside it: the marks on what changed, among the app's own
// layers, and the measures the recorder takes. `installStage` runs on the page around it, the
// stage: the page is the shot, and the camera moves the room in it with a transform. It holds
// the fade, the caption band at its foot, the cursor and a click's ring. The
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

  // A change shown where it happens, after Stageshow's motion rules ("every change moves",
  // "change only what changed", "no holes or blank frames"). `capture` notes the page as it is;
  // after the app has re-rendered, `morph` animates the difference: a word that changed rolls (the
  // old slides up and out, the new rises in, its span easing to its new width, as Stageshow's
  // Roll does), a block that moved slides to its new place, a new block fades in with a small
  // rise, and text that went fades out where it was. Web animations, so the film's clock moves
  // them frame by frame.
  const pathOf = element => {
    const parts = [];
    // From the nearest element with an id (the panel, the main column), so things added
    // elsewhere in the page don't shift it.
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      if (node.id) {
        parts.unshift(`#${node.id}`);
        break;
      }
      const key = node.getAttribute("data-person") ? `[${node.getAttribute("data-person")}]` : "";
      parts.unshift(`${node.tagName}${key}.${[...node.classList].sort().join(".")}:${[...(node.parentElement?.children || [])].indexOf(node)}`);
    }
    return parts.join(">");
  };
  const snapshot = () => {
    const leaves = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent.trim();
      const element = node.parentElement;
      if (!text || !element || element.closest(`${ours}, script, style`)) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const box = range.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      const style = getComputedStyle(element);
      leaves.push({node, text, box, path: `${pathOf(element)}#${[...element.childNodes].indexOf(node)}`,
        look: {font: style.font, color: style.color, letterSpacing: style.letterSpacing}});
    }
    const blocks = [];
    for (const element of document.body.querySelectorAll("*")) {
      if (element.closest(ours)) continue;
      if (!/block|flex|grid|table-row|list-item/.test(getComputedStyle(element).display)) continue;
      const key = element.textContent.replace(/\s+/g, " ").trim();
      const box = element.getBoundingClientRect();
      if (key && box.width && box.height) blocks.push({element, key, box, path: pathOf(element)});
    }
    // A block that says no more than the block around it is that block: keep the outermost, so a
    // section and its heading don't make each other look like twins.
    const kept = blocks.filter(block => !blocks.some(other => other !== block && other.key === block.key && other.element.contains(block.element)));
    return {leaves, blocks: kept};
  };
  const once = items => {
    const count = new Map();
    for (const item of items) count.set(item.key ?? item.text, (count.get(item.key ?? item.text) || 0) + 1);
    return item => count.get(item.key ?? item.text) === 1;
  };
  film.capture = () => {
    film.before = snapshot();
  };
  film.morph = () => {
    const before = film.before;
    const after = snapshot();
    film.before = null;
    if (!before) return {rolled: 0, moved: 0, entered: 0, left: 0};
    const SETTLE = "cubic-bezier(0.22, 1, 0.36, 1)";

    // Blocks the same in both (by their whole text, said once in each, or else by their place in
    // the page, when what they say changed): those that moved slide from where they were. One
    // inside another moves only by its own difference, so the two never add up.
    const lone = once(before.blocks), loneAfter = once(after.blocks);
    const was = new Map(before.blocks.filter(lone).map(block => [block.key, block]));
    const wasAt = new Map(before.blocks.map(block => [block.path, block]));
    // By text first, across the page, then by place for the rest, so a new row in an old row's
    // place isn't taken for it.
    const taken = new Set(), pairs = new Map();
    for (const block of after.blocks) {
      const old = loneAfter(block) && was.get(block.key);
      if (old && !taken.has(old)) {
        taken.add(old);
        pairs.set(block, old);
      }
    }
    for (const block of after.blocks) {
      if (pairs.has(block)) continue;
      const old = wasAt.get(block.path);
      if (old && !taken.has(old)) {
        taken.add(old);
        pairs.set(block, old);
      }
    }
    const moved = [...pairs].map(([block, old]) => ({element: block.element, dx: old.box.left - block.box.left, dy: old.box.top - block.box.top}));
    // Words: the same text said once in each is the same word; otherwise the same place in the
    // page is. A word whose place now says something else rolls.
    const loneLeaf = once(before.leaves), loneLeafAfter = once(after.leaves);
    const byText = new Map(before.leaves.filter(loneLeaf).map(leaf => [leaf.text, leaf]));
    const byPath = new Map(before.leaves.map(leaf => [leaf.path, leaf]));
    const used = new Set(), rolled = [], fresh = [], paired = new Set();
    // Words said once in both are paired first, across the whole page, so a row that moved down
    // keeps its words and a new row in its old place doesn't roll out of it.
    for (const leaf of after.leaves) {
      const twin = loneLeafAfter(leaf) && byText.get(leaf.text);
      if (twin && !used.has(twin)) {
        used.add(twin);
        paired.add(leaf);
      }
    }
    for (const leaf of after.leaves) {
      if (paired.has(leaf)) continue;
      const old = byPath.get(leaf.path);
      if (old && !used.has(old)) {
        used.add(old);
        if (old.text !== leaf.text) rolled.push({leaf, old});
        continue;
      }
      fresh.push(leaf);
    }
    const slides = [];
    for (const item of moved) {
      const outer = moved.filter(other => other !== item && other.element.contains(item.element))
        .sort((a, b) => (a.element.contains(b.element) ? 1 : -1))[0];
      // What holds a rolling word only slides up or down: its width is the roll's to ease.
      const rolling = rolled.some(({leaf}) => item.element.contains(leaf.node));
      const dx = rolling ? 0 : item.dx - (outer?.dx || 0), dy = item.dy - (outer?.dy || 0);
      if (Math.hypot(dx, dy) >= 0.5) slides.push({element: item.element, dx, dy});
    }
    // They close up once what left has faded, so nothing moves through words still going.
    for (const {element, dx, dy} of slides) {
      element.animate([{transform: `translate(${dx}px, ${dy}px)`}, {transform: "none"}], {duration: 500, delay: 120, easing: SETTLE, fill: "backwards"});
    }

    for (const {leaf, old} of rolled) {
      const roll = document.createElement("span");
      roll.style.cssText = "display: inline-grid; vertical-align: bottom; clip-path: inset(0 -100vw); white-space: pre;";
      const out = document.createElement("span");
      const into = document.createElement("span");
      out.textContent = old.node.textContent;
      into.textContent = leaf.node.textContent;
      for (const part of [out, into]) part.style.cssText = "grid-area: 1 / 1; justify-self: start; width: max-content;";
      roll.append(out, into);
      leaf.node.replaceWith(roll);
      const from = old.box.width, to = into.getBoundingClientRect().width;
      out.animate([{transform: "none", opacity: 1, filter: "blur(0)"}, {transform: "translateY(-100%)", opacity: 0, filter: "blur(2px)"}],
        {duration: 280, easing: "cubic-bezier(0.55, 0, 1, 0.45)", fill: "forwards"});
      into.animate([{transform: "translateY(100%)", opacity: 0, filter: "blur(2px)"}, {transform: "none", opacity: 1, filter: "blur(0)"}],
        {duration: 550, delay: 110, easing: SETTLE, fill: "backwards"});
      // The span eases to its new width, so what follows moves with it; then it is plain text again.
      roll.animate([{width: `${from}px`}, {width: `${to}px`}], {duration: 500, easing: SETTLE})
        .finished.then(() => roll.isConnected && roll.replaceWith(document.createTextNode(leaf.node.textContent)));
    }

    // New blocks, the outermost whose words are all new, fade in with a small rise as the rows
    // around them make room.
    const freshNodes = new Set(fresh.map(leaf => leaf.node));
    const novel = after.blocks.filter(block => {
      const inside = after.leaves.filter(leaf => block.element.contains(leaf.node));
      return inside.length && inside.every(leaf => freshNodes.has(leaf.node));
    });
    const arrivals = novel.filter(block => !novel.some(other => other !== block && other.element.contains(block.element)));
    for (const {element} of arrivals) {
      // It arrives as what it replaces leaves: once the rows making room for it have mostly gone.
      element.animate([{opacity: 0, transform: "translateY(8px)"}, {opacity: 1, transform: "none"}], {duration: 350, delay: 420, easing: SETTLE, fill: "backwards"});
    }
    const covered = new Set(arrivals.flatMap(({element}) => fresh.filter(leaf => element.contains(leaf.node)).map(leaf => leaf.node)));
    for (const leaf of fresh.filter(leaf => !covered.has(leaf.node))) {
      leaf.node.parentElement?.animate([{opacity: 0}, {opacity: 1}], {duration: 300, delay: 420, easing: SETTLE, fill: "backwards"});
    }

    // What went fades out where it was.
    const gone = before.leaves.filter(leaf => !used.has(leaf));
    for (const leaf of gone) {
      const ghost = document.createElement("span");
      ghost.textContent = leaf.node.textContent;
      Object.assign(ghost.style, {position: "fixed", left: `${leaf.box.left}px`, top: `${leaf.box.top}px`, font: leaf.look.font, color: leaf.look.color,
        letterSpacing: leaf.look.letterSpacing, whiteSpace: "pre", pointerEvents: "none", zIndex: "62", margin: "0"});
      mount();
      marks.append(ghost);
      ghost.animate([{opacity: 1}, {opacity: 0}], {duration: 150, easing: "ease-out", fill: "forwards"}).finished.then(() => ghost.remove());
    }
    const name = element => `${element.tagName.toLowerCase()}.${[...element.classList].join(".")}`;
    return {rolled: rolled.map(({old, leaf}) => `${old.text} -> ${leaf.text}`), moved: slides.map(({element, dx, dy}) => `${name(element)} ${Math.round(dx)},${Math.round(dy)}`),
      entered: arrivals.map(({element}) => name(element)), left: gone.map(leaf => leaf.text)};
  };

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

export function installStage({backdrop = "#000", path = "/", size, dpr = 2} = {}) {
  if (window !== window.top || location.pathname !== path) return;
  const film = window.__film = window.__film || {};

  // The pointer and the pointing hand, after Stageshow's MacPointer and MacPointingHand (MIT): the
  // arrow's hotspot at its tip, the hand's at its fingertip.
  const POINTER = '<svg viewBox="10 7 13 19" width="100%" height="100%" fill="none"><g transform="translate(10 7)"><path d="m6.148 18.473 1.863-1.003 1.615-.839-2.568-4.816h4.332l-11.379-11.408v16.015l3.316-3.221z" fill="#fff"/><path d="m6.431 17 1.765-.941-2.775-5.202h3.604l-8.025-8.043v11.188l2.53-2.442z" fill="#000"/></g></svg>';
  const HAND = '<svg viewBox="0 0 32 32" width="100%" height="100%" fill="none"><path d="M10.6 6a1.7 1.7 0 0 1 3.4 0v6.6a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0V19c0 3.1-2 5.6-5 5.6h-2.6c-2 0-3.3-.9-4.4-2.4l-3.1-4.1c-.6-.9-.4-2 .5-2.5.8-.4 1.7-.2 2.3.5l1.1 1.4V6Z" fill="#fff" stroke="#000" stroke-width="1.1" stroke-linejoin="round"/><path d="M14 12.6v3.6M16.6 13.3v3M19.2 14v2.6" stroke="#000" stroke-width="1" stroke-linecap="round"/></svg>';

  // The page is the shot: `size` output px at `dpr` device px per CSS px. The picture is its top,
  // where the camera moves the room (the window on its backdrop) with a transform, so motion is
  // smooth to a fraction of a pixel and text is drawn straight at the output's resolution. The
  // caption band is its foot, still, whatever the camera does.
  const view = {width: size.width / dpr, height: size.height / dpr, picture: (size.height - size.band) / dpr, band: size.band / dpr};
  const unit = Math.min(size.width, size.height) / dpr;
  let root = null;
  // Frames drawn, and the cursor's kind: which glyph shows, which it came from and when.
  let frames = 0;
  const glyph = {kind: "pointer", from: null, at: -100};
  const parts = {};
  function mount() {
    if (root?.isConnected) return;
    root = document.createElement("div");
    root.setAttribute("data-film-overlay", "");
    root.innerHTML = `<style>
      [data-film-overlay] { position: fixed; inset: 0; z-index: 2147483647; pointer-events: none; overflow: hidden; }
      [data-film-overlay] * { box-sizing: border-box; }
      [data-film-fade] { position: absolute; left: 0; top: 0; width: 100%; height: ${view.picture}px; background: ${backdrop}; }
      /* The band takes the pointer while the hand travels, so nothing in the screen is hovered. */
      [data-film-band] { position: absolute; left: 0; top: ${view.picture}px; width: 100%; height: ${view.band}px; background: ${backdrop};
        display: grid; place-items: center; padding: 0 ${unit * 0.08}px; pointer-events: auto; }
      [data-film-band] p { margin: 0; text-align: center; text-wrap: balance; font-family: var(--sans, sans-serif); font-weight: 400; font-feature-settings: "kern";
        font-size: ${Math.round(unit * dpr * 0.037) / dpr}px; line-height: ${Math.round(unit * dpr * 0.046) / dpr}px; letter-spacing: -0.01em; }
      [data-film-lens] { position: absolute; left: 0; top: 0; width: 100%; height: ${view.picture}px; overflow: hidden; }
      [data-film-cursor] { position: absolute; left: 0; top: 0; }
      [data-film-ripples] > i { position: absolute; border-radius: 50%; }
    </style>
      <div data-film-fade></div>
      <div data-film-band><p></p></div>
      <div data-film-lens><div data-film-ripples></div><div data-film-cursor="pointer">${POINTER}</div><div data-film-cursor="hand">${HAND}</div></div>`;
    document.body.append(root);
    for (const name of ["fade", "band", "lens", "ripples"]) parts[name] = root.querySelector(`[data-film-${name}]`);
    parts.words = parts.band.querySelector("p");
    parts.pointer = root.querySelector('[data-film-cursor="pointer"]');
    parts.hand = root.querySelector('[data-film-cursor="hand"]');
    parts.room = document.getElementById("room");
  }

  // Where a point of the room shows in the page, for the frame: the recorder's pointer goes there.
  film.toView = (frame, point) => {
    const s = view.width / frame.width;
    return {x: (point.x - frame.x) * s, y: (point.y - frame.y) * s};
  };
  // Where the pointer rests while the hand travels: on the band.
  film.parking = {x: view.width / 2, y: view.picture + view.band / 2};

  // state: {frame: {x, y, width, height} in the room's px, caption: {text, opacity, rise},
  // cursor: {x, y, visible, kind, press, opacity}, ripples: [{x, y, progress, fading}], fade}.
  film.render = state => {
    mount();
    const {frame} = state;
    // CSS px of the page per px of the room.
    const s = view.width / frame.width;
    parts.room.style.transform = `translate(${-frame.x * s}px, ${-frame.y * s}px) scale(${s})`;
    parts.fade.style.opacity = String(state.fade || 0);

    const caption = state.caption || {};
    parts.words.textContent = caption.text || "";
    Object.assign(parts.words.style, {color: getComputedStyle(document.body).color, opacity: String(caption.opacity ?? 0),
      transform: `translateY(${(caption.rise || 0) * unit * 0.02}px)`});

    // The cursor is drawn at macOS's size in the screen's own px, Stageshow's large style (1.5x),
    // so it grows with the camera as if filmed with the screen. A change of kind crosses over in
    // 0.2 s: the new glyph grows from its hotspot as the old one shrinks into it.
    const cursor = state.cursor || {};
    frames++;
    const kind = cursor.kind || "pointer";
    if (kind !== glyph.kind) Object.assign(glyph, {kind, from: glyph.kind, at: frames});
    const swap = Math.min(1, (frames - glyph.at) / 6);
    const grown = swap * swap * (3 - 2 * swap);
    const at = `${(cursor.x - frame.x) * s}px, ${(cursor.y - frame.y) * s}px`;
    // In screen px: the arrow 13 by 19 units at 0.75 px a unit, the hand 32 units at 0.8125.
    const LARGE = 1.5;
    const sizes = {pointer: {unit: 0.75 * LARGE * s, width: 13, height: 19, hot: [0, 0]}, hand: {unit: 0.8125 * LARGE * s, width: 32, height: 32, hot: [12, 4]}};
    for (const name of ["pointer", "hand"]) {
      const {unit: u, width, height, hot} = sizes[name];
      const share = name === glyph.kind ? grown : name === glyph.from ? 1 - grown : 0;
      Object.assign(parts[name].style, {
        width: `${width * u}px`, height: `${height * u}px`, display: cursor.visible && share > 0 ? "block" : "none",
        opacity: String((cursor.opacity ?? 1) * share), transformOrigin: `${hot[0] * u}px ${hot[1] * u}px`,
        transform: `translate(${at}) translate(${-hot[0] * u}px, ${-hot[1] * u}px) scale(${share * (cursor.press ?? 1)})`,
        filter: `drop-shadow(0 ${1 * LARGE * s}px ${2 * LARGE * s}px rgba(0, 0, 0, 0.3))`,
      });
    }

    // Stageshow's ring: 36 screen px, a 1.5 px edge in the accent over a tenth of it, from a fifth
    // of its size to all of it as it fades.
    parts.ripples.innerHTML = (state.ripples || []).map(ripple => {
      const width = 36 * s * (0.2 + 0.8 * ripple.progress);
      return `<i style="left:${(ripple.x - frame.x) * s - width / 2}px;top:${(ripple.y - frame.y) * s - width / 2}px;width:${width}px;height:${width}px;border:${1.5 * s}px solid rgb(var(--accent-deep-rgb, 153 175 98));background:rgb(var(--accent-deep-rgb, 153 175 98) / 10%);opacity:${(1 - ripple.progress) * (ripple.fading ?? 1)}"></i>`;
    }).join("");
  };
}
