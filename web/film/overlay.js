// What a film draws over the real dashboard: the cursor, a click's ripple, the caption band and
// the fade at a cut. The recorder sets every value each frame (nothing here animates by itself),
// and the layer is designed in output pixels, then scaled onto the part of the page in frame.
// Injected as an init script, so the function must stay self-contained.
export function installOverlay({screen, margin = 0, backdrop = "#000", theme = "dark"}) {
  const film = window.__film = window.__film || {};

  // The dashboard's screen: the body, a band's height shorter than the window and, with a margin,
  // set on the backdrop as a window with rounded corners and a hairline edge. Its transform makes
  // it the box fixed elements (toasts, hover cards, the person panel) sit in, so nothing the
  // dashboard shows ends up under the caption band. The document doesn't scroll; the screen does.
  const fit = document.createElement("style");
  const edge = theme === "light" ? "0 0 0 1px rgb(0 0 0 / 9%), 0 24px 64px rgb(0 0 0 / 10%)" : "0 0 0 1px rgb(255 255 255 / 9%)";
  // The dashboard's rail is a viewport tall (100svh), and the window is taller than the screen, so
  // it is held to the screen's height or its footer would run under the band.
  fit.textContent = `html { overflow: hidden !important; background: ${backdrop} !important; }
    body { position: relative; margin: ${margin}px !important; width: ${screen.width}px; height: ${screen.height}px !important; min-height: 0 !important; overflow: auto !important; transform: translateZ(0); scrollbar-width: none; ${margin ? `border-radius: 12px; box-shadow: ${edge};` : ""} }
    .app-shell { min-height: ${screen.height}px !important; }
    .rail { height: ${screen.height}px !important; }
    @media (max-width: 1100px) { .rail { height: var(--top-menu-height) !important; } }`;
  // Init scripts can run before the document has its <html>.
  const place = () => document.documentElement && (document.documentElement.append(fit), true);
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

  // The pointer and the pointing hand, after Stageshow's MacPointer and MacPointingHand (MIT): the
  // arrow's hotspot at its tip, the hand's at its fingertip. The hand draws a little larger, as on
  // macOS, and shows where the page's own cursor would be a pointer (links, buttons, rows).
  const POINTER = '<svg viewBox="10 7 13 19" width="100%" height="100%" fill="none"><g transform="translate(10 7)"><path d="m6.148 18.473 1.863-1.003 1.615-.839-2.568-4.816h4.332l-11.379-11.408v16.015l3.316-3.221z" fill="#fff"/><path d="m6.431 17 1.765-.941-2.775-5.202h3.604l-8.025-8.043v11.188l2.53-2.442z" fill="#000"/></g></svg>';
  const HAND = '<svg viewBox="0 0 32 32" width="100%" height="100%" fill="none"><path d="M10.6 6a1.7 1.7 0 0 1 3.4 0v6.6a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0v.7a1.3 1.3 0 0 1 2.6 0V19c0 3.1-2 5.6-5 5.6h-2.6c-2 0-3.3-.9-4.4-2.4l-3.1-4.1c-.6-.9-.4-2 .5-2.5.8-.4 1.7-.2 2.3.5l1.1 1.4V6Z" fill="#fff" stroke="#000" stroke-width="1.1" stroke-linejoin="round"/><path d="M14 12.6v3.6M16.6 13.3v3M19.2 14v2.6" stroke="#000" stroke-width="1" stroke-linecap="round"/></svg>';

  let root = null;
  let marks = null;
  const parts = {};
  function mount() {
    if (root?.isConnected && marks?.isConnected) return;
    root?.remove();
    marks?.remove();
    root = document.createElement("div");
    root.setAttribute("data-film-overlay", "");
    root.innerHTML = `<div data-film-stage>
      <div data-film-fade></div>
      <div data-film-band><p data-film-caption></p></div>
      <div data-film-foot></div>
      <div data-film-ripples></div>
      <div data-film-cursor="pointer">${POINTER}</div>
      <div data-film-cursor="hand">${HAND}</div>
    </div>`;
    const css = document.createElement("style");
    css.textContent = `
      [data-film-overlay] { position: fixed; z-index: 2147483647; pointer-events: none; overflow: hidden; contain: strict; }
      [data-film-overlay] * { box-sizing: border-box; }
      [data-film-stage] { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
      [data-film-fade], [data-film-band] { position: absolute; left: 0; right: 0; }
      [data-film-fade] { top: 0; bottom: 0; }
      /* The band takes the pointer while the hand travels, so nothing in the page is hovered. */
      [data-film-band] { bottom: 0; display: grid; place-items: center; pointer-events: auto; }
      [data-film-caption] { margin: 0; text-align: center; text-wrap: balance; font-family: var(--sans, sans-serif); font-weight: 400; font-feature-settings: "kern"; }
      [data-film-cursor] { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
      [data-film-ripples] > i { position: absolute; border-radius: 50%; border-style: solid; }
      [data-film-foot] { position: absolute; left: 0; right: 0; pointer-events: none; }
    `;
    root.append(css);
    document.documentElement.append(root);
    for (const name of ["stage", "fade", "band", "caption", "foot", "ripples"]) parts[name] = root.querySelector(`[data-film-${name}]`);
    parts.pointer = root.querySelector('[data-film-cursor="pointer"]');
    parts.hand = root.querySelector('[data-film-cursor="hand"]');
    // Highlights live in the page itself, among the app's own layers: under its toasts, over its
    // panel when they mark something in it, and inside what fades.
    marks = document.createElement("div");
    marks.setAttribute("data-film-layer", "");
    marks.innerHTML = '<div data-film-marks="under"></div><div data-film-marks="over"></div>';
    const markCss = document.createElement("style");
    markCss.textContent = `
      [data-film-marks] { position: absolute; left: 0; top: 0; width: 0; height: 0; pointer-events: none; }
      [data-film-marks="under"] { z-index: 9; }
      [data-film-marks="over"] { z-index: 61; }
      [data-film-marks] > i { position: absolute; border-radius: 5px; background: rgb(var(--accent-deep-rgb, 153 175 98) / 16%); box-shadow: inset 0 0 0 1.5px rgb(var(--accent-deep-rgb, 153 175 98) / 55%); }
    `;
    marks.append(markCss);
    document.body.append(marks);
  }

  // state: {frame: {x, y, width, height} in CSS px of the viewport, size: {width, height} in
  // output px, band (output px), caption: {text, opacity, rise}, cursor: {x, y, visible,
  // pressed} in CSS px, ripples: [{x, y, progress}], fade (0 to 1), zoom}.
  film.render = state => {
    mount();
    const {frame, size} = state;
    const k = size.width / frame.width;
    const ink = getComputedStyle(document.body).color;
    Object.assign(root.style, {left: `${frame.x}px`, top: `${frame.y}px`, width: `${frame.width}px`, height: `${frame.height}px`});
    Object.assign(parts.stage.style, {width: `${size.width}px`, height: `${size.height}px`, transform: `scale(${1 / k})`});
    const unit = Math.min(size.width, size.height);

    // The band, the fades and the margin are all the backdrop.
    Object.assign(parts.fade.style, {background: backdrop, opacity: String(state.fade || 0)});

    const band = state.band || 0;
    Object.assign(parts.band.style, {height: `${band}px`, background: backdrop, display: band ? "grid" : "none", padding: `0 ${Math.round(unit * 0.08)}px`});
    const caption = state.caption || {};
    parts.caption.textContent = caption.text || "";
    Object.assign(parts.caption.style, {
      color: ink, opacity: String(caption.opacity ?? 0),
      fontSize: `${Math.round(unit * 0.037)}px`, lineHeight: `${Math.round(unit * 0.046)}px`, letterSpacing: "-0.01em",
      transform: `translateY(${(caption.rise || 0) * unit * 0.02}px)`,
    });

    // In the body's own coordinates: it is the screen, the box these sit in, and it may scroll.
    // The fade covers them as it covers the page.
    const body = document.body.getBoundingClientRect();
    const dx = document.body.scrollLeft - body.left, dy = document.body.scrollTop - body.top;
    for (const layer of ["under", "over"]) {
      marks.querySelector(`[data-film-marks="${layer}"]`).innerHTML = (state.highlights || []).filter(mark => (mark.over ? "over" : "under") === layer)
        .map(mark => `<i style="left:${mark.x + dx}px;top:${mark.y + dy}px;width:${mark.width}px;height:${mark.height}px;opacity:${mark.opacity}"></i>`).join("");
    }

    // Where the frame cuts through the screen above the band, the screen fades into it, all the
    // way before the edge, and more as more of the screen runs on below. Short enough to stay
    // clear of a toast at the screen's foot (24px up).
    const footHeight = band ? Math.round(Math.min(unit * 0.05, 22 * k)) : 0;
    const pictureFoot = frame.y + (size.height - band) / k;
    const below = margin + screen.height - pictureFoot;
    Object.assign(parts.foot.style, {bottom: `${band}px`, height: `${footHeight}px`, background: `linear-gradient(to bottom, transparent, ${backdrop} 75%)`,
      opacity: String(Math.max(0, Math.min(1, below / 22)))});

    // The hand where the page's own cursor would be a pointer, as a real one works.
    const cursor = state.cursor || {};
    const under = cursor.visible && document.elementsFromPoint(cursor.x, cursor.y).find(element => !element.closest("[data-film-overlay], [data-film-layer]"));
    const hand = Boolean(under && getComputedStyle(under).cursor === "pointer");
    const tall = unit * 0.05 * (state.zoom || 1);
    const at = `${(cursor.x - frame.x) * k}px, ${(cursor.y - frame.y) * k}px`;
    const press = `scale(${cursor.pressed ? 0.9 : 1})`;
    Object.assign(parts.pointer.style, {
      width: `${tall * 13 / 19}px`, height: `${tall}px`, display: cursor.visible && !hand ? "block" : "none", opacity: String(cursor.opacity ?? 1),
      transform: `translate(${at}) ${press}`,
    });
    // 32 units at 26/24 the arrow's scale; the fingertip is at (12, 4).
    const unitPx = (tall / 19) * (26 / 24);
    Object.assign(parts.hand.style, {
      width: `${32 * unitPx}px`, height: `${32 * unitPx}px`, display: cursor.visible && hand ? "block" : "none", opacity: String(cursor.opacity ?? 1),
      transformOrigin: `${12 * unitPx}px ${4 * unitPx}px`, transform: `translate(${at}) translate(${-12 * unitPx}px, ${-4 * unitPx}px) ${press}`,
    });

    parts.ripples.innerHTML = (state.ripples || []).map(ripple => {
      const size = unit * 0.07 * (state.zoom || 1) * (0.2 + 0.8 * ripple.progress);
      return `<i style="left:${(ripple.x - frame.x) * k - size / 2}px;top:${(ripple.y - frame.y) * k - size / 2}px;width:${size}px;height:${size}px;border-width:${Math.max(1.5, unit * 0.0025)}px;border-color:${ink};opacity:${(1 - ripple.progress) * 0.7 * (ripple.fading ?? 1)}"></i>`;
    }).join("");
  };

  // Whether what is on top at a point (leaving out the film's own layers) is, or is inside, a match.
  film.hits = (selectors, x, y) => {
    const top = document.elementsFromPoint(x, y).find(element => !element.closest("[data-film-overlay], [data-film-layer]"));
    return Boolean(top?.closest([].concat(selectors).join(", ")));
  };

  // Where something is on screen: the union of every match, in CSS px of the viewport. With
  // `text`, only what is written inside them, so a full-width row frames as its words.
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

  // The smallest text in frame, at output size: every visible text run the camera shows, leaving
  // out what a panel covers. Everything is made hit-testable for the check, so a toast counts.
  film.textSizes = (frame, scale) => {
    const found = [];
    const probe = document.createElement("style");
    probe.textContent = "* { pointer-events: auto !important; }";
    document.documentElement.append(probe);
    const top = (x, y) => document.elementsFromPoint(x, y).find(element => !element.closest("[data-film-overlay], [data-film-layer]"));
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent.trim();
      const element = node.parentElement;
      if (!text || !element || element.closest("[data-film-overlay], [data-film-layer]")) continue;
      const style = getComputedStyle(element);
      if (style.visibility === "hidden" || Number(style.opacity) === 0) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const box = range.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      const inside = box.left >= frame.x - 1 && box.top >= frame.y - 1 && box.right <= frame.x + frame.width + 1 && box.bottom <= frame.y + frame.height + 1;
      if (!inside) continue;
      const over = top(box.left + box.width / 2, box.top + box.height / 2);
      if (over && !element.contains(over) && !over.contains(element)) continue;
      found.push({text: text.slice(0, 60), size: Math.round(parseFloat(style.fontSize) * scale * 10) / 10});
    }
    probe.remove();
    return found;
  };
}
