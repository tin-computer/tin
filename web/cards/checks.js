// Checks that run inside a card page. Each returns plain data; tin-cards.js turns it into the
// errors the agent fixes before uploading.
//
// A card is one <main data-card> at 1080 x 1350. Its headline is marked data-card-line, a logo
// data-card-logo and a product screen or terminal data-card-screen. The words a card carries are
// its headline and nothing else; a screen's own words are the product's and don't count. Every
// colour it paints is one of the brand's.

export const WIDTH = 1080;
export const HEIGHT = 1350;

// Fonts every visible text uses, and whether the page loaded them. A generic or system family
// (sans-serif, ui-monospace, -apple-system) is a fallback: a card set in one looks unbranded.
// scope is the card, or "body" for a product page shot for one.
export function fonts(scope = "[data-card]") {
  const clean = value => value.trim().replace(/^["']|["']$/g, "");
  const loaded = new Set([...document.fonts].filter(face => face.status === "loaded").map(face => clean(face.family)));
  const generic = /^(serif|sans-serif|monospace|cursive|fantasy|system-ui|ui-sans-serif|ui-serif|ui-monospace|ui-rounded|-apple-system|BlinkMacSystemFont|emoji|math)$/i;
  const found = new Map();
  for (const element of document.querySelectorAll(`${scope} *`)) {
    const text = [...element.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent).join("").trim();
    if (!text) continue;
    const box = element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    const family = clean(getComputedStyle(element).fontFamily.split(",")[0]);
    if (found.has(family)) continue;
    found.set(family, {family, loaded: loaded.has(family), generic: generic.test(family), sample: text.slice(0, 40)});
  }
  return [...found.values()];
}

// The card's own words, outside its screens and logo, and where they sit. Text past the card's
// edge, or clipped by a box that hides overflow, is reported with the words it cuts.
export function words() {
  const card = document.querySelector("[data-card]");
  if (!card) return {missing: true};
  const frame = card.getBoundingClientRect();
  const items = [];
  // Measure the words themselves: an element's box can run past an edge its text never reaches.
  const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent.replace(/\s+/g, " ").trim();
    if (!text) continue;
    const element = node.parentElement;
    const style = getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) < 0.05) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0);
    if (!rects.length) continue;
    const box = {
      left: Math.min(...rects.map(rect => rect.left)), top: Math.min(...rects.map(rect => rect.top)),
      right: Math.max(...rects.map(rect => rect.right)), bottom: Math.max(...rects.map(rect => rect.bottom)),
    };
    const inScreen = Boolean(element.closest("[data-card-screen]"));
    const inLogo = Boolean(element.closest("[data-card-logo]"));
    const line = Boolean(element.closest("[data-card-line]"));
    const outside = box.left < frame.left - 1 || box.top < frame.top - 1 || box.right > frame.right + 1 || box.bottom > frame.bottom + 1;
    let clipped = false;
    for (let parent = element; parent && parent !== card; parent = parent.parentElement) {
      const parentStyle = getComputedStyle(parent);
      if (!/(hidden|clip)/.test(parentStyle.overflow + parentStyle.overflowX + parentStyle.overflowY)) continue;
      const edge = parent.getBoundingClientRect();
      if (box.right > edge.right + 1 || box.bottom > edge.bottom + 1 || box.left < edge.left - 1 || box.top < edge.top - 1) clipped = true;
    }
    items.push({text: text.slice(0, 80), inScreen, inLogo, line, outside, clipped});
  }
  const headline = card.querySelector("[data-card-line]");
  return {
    items,
    headline: headline ? headline.innerText.replace(/\s+/g, " ").trim() : null,
    size: {width: Math.round(frame.width), height: Math.round(frame.height)},
  };
}

// Every colour the card paints with CSS: text, fills, borders, outlines and SVG paint, with
// where it was seen. Images (artwork, a screen shot from the product) are pixels, not CSS, and
// keep their own colours. tin-cards.js compares each with the brand's palette.
export function colours() {
  const seen = new Map();
  const add = (value, what, element) => {
    if (!value || value === "none" || value === "transparent" || /^rgba\(.*,\s*0\)$/.test(value)) return;
    const key = value.replace(/\s+/g, "");
    if (seen.has(key)) return;
    const text = (element.innerText || element.textContent || element.className || element.tagName).toString();
    seen.set(key, {value, what, sample: text.replace(/\s+/g, " ").trim().slice(0, 40) || element.tagName.toLowerCase()});
  };
  for (const element of [document.querySelector("[data-card]"), ...document.querySelectorAll("[data-card] *")]) {
    if (!element) continue;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) < 0.05 || box.width < 1 || box.height < 1) continue;
    const ownText = [...element.childNodes].some(node => node.nodeType === 3 && node.textContent.trim());
    if (ownText) add(style.color, "text", element);
    add(style.backgroundColor, "fill", element);
    for (const match of style.backgroundImage.matchAll(/(rgba?\([^)]*\)|#[0-9a-f]{3,8})/gi)) add(match[1], "gradient", element);
    for (const side of ["Top", "Right", "Bottom", "Left"]) {
      if (parseFloat(style[`border${side}Width`]) > 0 && style[`border${side}Style`] !== "none") add(style[`border${side}Color`], "border", element);
    }
    if (parseFloat(style.outlineWidth) > 0 && style.outlineStyle !== "none") add(style.outlineColor, "outline", element);
    if (element instanceof SVGElement) {
      add(style.fill, "fill", element);
      add(style.stroke, "stroke", element);
    }
  }
  return [...seen.values()];
}

// A bar down the left edge of a box: a left border at least 2 px and wider than the box's
// other borders, a quote's rule included. Tin marks nothing that way; a label or the content
// says what a box is. scope is the card, or "body" with the region (a box in page pixels) a
// product page is shot from, where the bar is the product's own and the stage removes it.
export function leftBars({scope = "[data-card]", region = null} = {}) {
  const found = [];
  for (const element of document.querySelectorAll(`${scope} *`)) {
    const style = getComputedStyle(element);
    if (style.borderLeftStyle === "none" || style.display === "none" || style.visibility === "hidden") continue;
    const left = parseFloat(style.borderLeftWidth);
    const others = Math.max(parseFloat(style.borderTopWidth), parseFloat(style.borderRightWidth), parseFloat(style.borderBottomWidth));
    if (!(left >= 2 && left > others)) continue;
    const box = element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    if (region && !(box.right > region.x && box.left < region.x + region.width && box.bottom > region.y && box.top < region.y + region.height)) continue;
    found.push({text: (element.innerText || element.tagName).replace(/\s+/g, " ").trim().slice(0, 40)});
  }
  return found;
}

// Each visible text run inside the region (a box in page pixels) and its size, so a shot can be
// held to the smallest size a card can carry once the shot is enlarged on it.
export function textSizes(region) {
  const found = new Map();
  // Only the shot's own words: on a site, a panel lifted out of its slot sits over other text.
  const root = (region.selector && document.querySelector(region.selector)) || document.body;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent.replace(/\s+/g, " ").trim();
    if (!text) continue;
    const style = getComputedStyle(node.parentElement);
    if (style.visibility === "hidden" || Number(style.opacity) < 0.05) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const seen = [...range.getClientRects()].some(rect => rect.width > 0 && rect.height > 0
      && rect.right > region.x && rect.left < region.x + region.width && rect.bottom > region.y && rect.top < region.y + region.height);
    if (!seen) continue;
    const size = parseFloat(style.fontSize);
    if (!found.has(text) || found.get(text).size > size) found.set(text, {text: text.slice(0, 40), size});
  }
  return [...found.values()].sort((a, b) => a.size - b.size);
}

// Each screen's box on the card, for figureGround.
export function screenBoxes() {
  const card = document.querySelector("[data-card]").getBoundingClientRect();
  return [...document.querySelectorAll("[data-card-screen]")].map(screen => {
    const box = screen.getBoundingClientRect();
    return {x: box.left - card.left, y: box.top - card.top, width: box.width, height: box.height,
      sample: screen.querySelector("img")?.getAttribute("src") || "the screen"};
  });
}

// A screen stands off the ground it sits on: a light one on a dark ground or artwork, a dark one
// on a light ground. Compares the rendered card just inside each window's edge with just outside
// it, where the card shows; a pale window on pale paper reads as one muted surface.
export async function figureGround({src, boxes, scale}) {
  const image = new Image();
  image.src = src;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const context = canvas.getContext("2d", {willReadFrequently: true});
  context.drawImage(image, 0, 0);
  const linear = value => {
    const v = value / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  const luminance = strips => {
    let total = 0;
    let count = 0;
    for (const [x, y, w, h] of strips) {
      const left = Math.max(0, Math.round(x * scale)), top = Math.max(0, Math.round(y * scale));
      const right = Math.min(canvas.width, Math.round((x + w) * scale)), bottom = Math.min(canvas.height, Math.round((y + h) * scale));
      if (right - left < 1 || bottom - top < 1) continue;
      const data = context.getImageData(left, top, right - left, bottom - top).data;
      for (let i = 0; i < data.length; i += 4) {
        total += 0.2126 * linear(data[i]) + 0.7152 * linear(data[i + 1]) + 0.0722 * linear(data[i + 2]);
        count += 1;
      }
    }
    return count ? total / count : null;
  };
  const ring = (box, from, to) => {
    // A band between from and to px from the box's edge: positive inside, negative outside.
    const [a, b] = [Math.min(from, to), Math.max(from, to)];
    const w = b - a;
    return [
      [box.x + a, box.y + a, box.width - 2 * a, w],
      [box.x + a, box.y + box.height - b, box.width - 2 * a, w],
      [box.x + a, box.y + b, w, box.height - 2 * b],
      [box.x + box.width - b, box.y + b, w, box.height - 2 * b],
    ];
  };
  return boxes.map(box => {
    const inside = luminance(ring(box, 6, 18));
    const outside = luminance(ring(box, -36, -12));
    if (inside === null || outside === null) return {sample: box.sample, ratio: null};
    const ratio = (Math.max(inside, outside) + 0.05) / (Math.min(inside, outside) + 0.05);
    return {sample: box.sample, ratio: Math.round(ratio * 100) / 100};
  });
}

// What a stage script may not change inside a screen: its type and the space inside it. A screen
// keeps the product's own scale and spacing, so its parts stay grouped as the product groups them;
// the shot is enlarged as a whole on the card instead. Kept before the script runs, compared after.
// Both run in the page, so each names the spacing it compares.
export function keepStyles() {
  const SPACING = ["marginTop", "marginBottom", "paddingTop", "paddingBottom", "paddingLeft", "paddingRight", "rowGap", "columnGap"];
  const kept = new WeakMap();
  for (const element of document.querySelectorAll("body *")) {
    const style = getComputedStyle(element);
    kept.set(element, {size: style.fontSize, line: style.lineHeight, space: SPACING.map(name => style[name])});
  }
  window.__tinKept = kept;
}

// The window's own frame (the shot's element, and any element filling it exactly) may even out its
// padding; nothing inside it may change size of type or space.
export function restyled(selector) {
  const SPACING = ["marginTop", "marginBottom", "paddingTop", "paddingBottom", "paddingLeft", "paddingRight", "rowGap", "columnGap"];
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root || !window.__tinKept) return [];
  const edge = root.getBoundingClientRect();
  const frame = element => {
    const box = element.getBoundingClientRect();
    return Math.abs(box.width - edge.width) < 2 && Math.abs(box.height - edge.height) < 2;
  };
  const px = value => parseFloat(value) || 0;
  const sample = element => (element.innerText || element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40) || element.className || element.tagName.toLowerCase();
  const found = [];
  for (const element of [root, ...root.querySelectorAll("*")]) {
    const kept = window.__tinKept.get(element);
    const style = getComputedStyle(element);
    if (!kept || style.display === "none" || style.visibility === "hidden") continue;
    if (element.textContent.trim() && (kept.size !== style.fontSize || (kept.line !== style.lineHeight && kept.line !== "normal"))) {
      found.push({element, kind: "type", text: sample(element), from: `${kept.size}/${kept.line}`, to: `${style.fontSize}/${style.lineHeight}`});
      continue;
    }
    if (element === root || frame(element)) continue;
    const moved = SPACING.filter((name, index) => Math.abs(px(style[name]) - px(kept.space[index])) > 1);
    if (moved.length) found.push({element, kind: "space", text: sample(element), what: moved.map(name => `${name} ${kept.space[SPACING.indexOf(name)]} → ${style[name]}`).join(", ")});
  }
  // Type set on a box changes every word inside it; name the box once.
  const typed = found.filter(item => item.kind === "type").map(item => item.element);
  return found
    .filter(item => item.kind !== "type" || !typed.some(other => other !== item.element && other.contains(item.element)))
    .map(({element, ...item}) => item);
}

// Where each screen sits in the frame that clips it (the card, or a panel with overflow hidden).
// A screen either runs off an edge of that frame or is centred in it; one that floats whole with
// unequal sides reads as misplaced. A satellite, a smaller screen set over a larger one's corner,
// is placed against that screen instead.
export function placement() {
  const card = document.querySelector("[data-card]");
  const screens = [...document.querySelectorAll("[data-card-screen]")];
  const area = box => box.width * box.height;
  const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  return screens.map(screen => {
    let frame = card;
    for (let parent = screen.parentElement; parent && parent !== card; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (/(hidden|clip)/.test(style.overflow + style.overflowX)) { frame = parent; break; }
    }
    const box = screen.getBoundingClientRect();
    const edge = frame.getBoundingClientRect();
    const satellite = screens.some(other => other !== screen && overlaps(box, other.getBoundingClientRect()) && area(other.getBoundingClientRect()) > area(box));
    return {
      left: Math.round(box.left - edge.left), right: Math.round(edge.right - box.right), satellite,
      frame: frame === card ? "the card" : "its panel",
      sample: (screen.innerText || screen.querySelector("img")?.getAttribute("src") || "the screen").replace(/\s+/g, " ").trim().slice(0, 40),
    };
  });
}

// A window larger than the shot it holds: inside the card, an empty strip of window reads as a box
// left open. A window is the screen's own size; where it runs off the card, its content does.
export function emptyWindows() {
  const card = document.querySelector("[data-card]").getBoundingClientRect();
  const visible = box => ({left: Math.max(box.left, card.left), top: Math.max(box.top, card.top),
    right: Math.min(box.right, card.right), bottom: Math.min(box.bottom, card.bottom)});
  return [...document.querySelectorAll("[data-card-screen]")].flatMap(screen => {
    const image = screen.querySelector(":scope > img");
    if (!image) return [];
    const window = visible(screen.getBoundingClientRect());
    const shot = visible(image.getBoundingClientRect());
    // A border's width is not empty window.
    const gaps = {right: window.right - shot.right, below: window.bottom - shot.bottom};
    const open = Object.entries(gaps).filter(([, gap]) => gap > 4).map(([side, gap]) => `${Math.round(gap)} px ${side === "right" ? "to its right" : "below it"}`);
    return open.length ? [{sample: image.getAttribute("src") || "the screen", open}] : [];
  });
}

// Eyebrows, the small label set above a title or a card's words (uppercase, letter-spaced or
// mono: "HOW IT WORKS"). Tin never uses them, on a card or in a screen on it. A table's column
// headers are not eyebrows, and neither is data set in mono, such as a date or a time.
export function eyebrows() {
  const own = element => [...element.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent).join("").replace(/\s+/g, " ").trim();
  const parts = [...document.querySelectorAll("[data-card] *")].flatMap(element => {
    const text = own(element);
    if (!text || element.closest("th, thead, [role=columnheader]")) return [];
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (box.width < 2 || box.height < 2 || style.visibility === "hidden" || style.display === "none" || Number(style.opacity) < 0.05) return [];
    const size = parseFloat(style.fontSize);
    const letters = text.replace(/[^A-Za-z]/g, "");
    return [{text, size, top: box.top, bottom: box.bottom, left: box.left, right: box.right,
      upper: style.textTransform === "uppercase" || (letters.length >= 3 && letters === letters.toUpperCase()),
      spaced: parseFloat(style.letterSpacing) / size >= 0.04,
      mono: /mono|courier|consolas|menlo|monaco/i.test(style.fontFamily)}];
  });
  const found = [];
  for (const label of parts) {
    if (label.text.split(" ").length > 6 || /[.!?:]$/.test(label.text)) continue;
    if (!(label.upper || label.spaced || label.mono)) continue;
    if (!label.upper && !label.spaced && /\d/.test(label.text)) continue;
    const lined = other => Math.abs(other.left - label.left) <= 3 || Math.abs((other.left + other.right) / 2 - (label.left + label.right) / 2) <= 3;
    const under = parts.filter(other => other !== label && other.top >= label.bottom - 2 && other.top - label.bottom <= Math.max(28, label.size * 2.6)
      && other.left < label.right && other.right > label.left && other.size >= label.size * 1.2 && lined(other))
      .sort((a, b) => a.top - b.top)[0];
    if (under) found.push({text: label.text, under: under.text.slice(0, 40)});
  }
  return found;
}
