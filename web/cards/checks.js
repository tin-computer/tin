// Checks that run inside a card page. Each returns plain data; tin-cards.js turns it into the
// errors the agent fixes before uploading.
//
// A card is one <main data-card> at 1080 x 1350. Its headline is marked data-card-line, a logo
// data-card-logo and a product screen or terminal data-card-screen. The words a card carries are
// its headline and nothing else; a screen's own words are the product's and don't count.

export const WIDTH = 1080;
export const HEIGHT = 1350;

// Fonts every visible text uses, and whether the page loaded them. A generic or system family
// (sans-serif, ui-monospace, -apple-system) is a fallback: a card set in one looks unbranded.
export function fonts() {
  const clean = value => value.trim().replace(/^["']|["']$/g, "");
  const loaded = new Set([...document.fonts].filter(face => face.status === "loaded").map(face => clean(face.family)));
  const generic = /^(serif|sans-serif|monospace|cursive|fantasy|system-ui|ui-sans-serif|ui-serif|ui-monospace|ui-rounded|-apple-system|BlinkMacSystemFont|emoji|math)$/i;
  const found = new Map();
  for (const element of document.querySelectorAll("[data-card] *")) {
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
