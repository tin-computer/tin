// One composer for every company's cards. A brand profile holds what differs between companies:
// grounds, headline type and how its last words turn, the signature, artwork or an edge, and how a
// window is framed. A layout holds what doesn't: where the headline, a band and the window go. A
// batch names its profile and, for each post, a layout, a ground, the headline and a shot from
// `tin-cards shoot`, whose size sets the window's (see guide/README.md, Compose).

import fs from "node:fs";
import path from "node:path";
import {HEIGHT as H, WIDTH as W} from "./checks.js";

const GUTTER = 72;

export class ComposeError extends Error {}

const escape = text => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// A shot's size in pixels, from its PNG header; `shoot` writes PNG.
function pngSize(file) {
  const bytes = fs.readFileSync(file);
  if (bytes.length < 24 || bytes.toString("latin1", 1, 4) !== "PNG") throw new ComposeError(`${file} isn't a PNG. Shoot screens with tin-cards shoot.`);
  return {width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20)};
}

function head(b) {
  const links = (b.stylesheets || []).map(href => `<link rel="stylesheet" href="${b.url(href)}">`).join("");
  const faces = (b.fonts || []).map(f => `@font-face{font-family:'${f.family}';src:url("${b.url(f.src)}");font-weight:${f.weight || 400};font-style:${f.style || "normal"};font-display:block}`).join("");
  const reset = `*{box-sizing:border-box} html,body{margin:0;width:${W}px;height:${H}px;background:none} main{position:relative;width:${W}px;height:${H}px;overflow:hidden}`;
  return `<meta charset="utf-8">${links}<style>${faces}${reset}</style>`;
}

// The headline's colours on a ground: artwork takes the colours of the ground it reads like.
function tone(b, theme) {
  const key = theme === "art-light" ? "light" : theme === "art" ? "dark" : theme;
  if (!b.headline[key]) throw new ComposeError(`The profile's headline has no colours for the ${key} ground.`);
  return b.headline[key];
}

function headline(b, post, theme, top, {left = GUTTER, width = W - 2 * GUTTER, align = "left"} = {}) {
  const t = b.headline;
  const c = tone(b, theme);
  const style = `position:absolute; left:${left}px; top:${top}px; width:${width}px; margin:0; text-align:${align}; font-family:${t.family}; `
    + `font-weight:${t.weight}; font-size:${t.size}px; line-height:${t.line}; letter-spacing:${t.track}; text-wrap:balance`;
  const turn = post.turn ? ` <span style="color:${c.turn}; font-style:${c.turn_style || "normal"}">${escape(post.turn)}</span>` : "";
  return `<h1 class="line" data-card-line style="${style}"><span style="color:${c.lit}">${escape(post.lit)}</span>${turn}</h1>`;
}

const headlineHeight = (b, lines = 2) => Math.round(lines * b.headline.size * b.headline.line);

const logoTheme = theme => (theme === "light" || theme === "art-light" ? "light" : "dark");

// The logo at the top, or at the foot with the address opposite. Returns the HTML and the room
// it takes at the foot.
function signature(b, theme, place) {
  const logo = b.logo;
  const lt = logoTheme(theme);
  const filter = lt === "dark" && logo.filter_dark ? `filter:${logo.filter_dark};` : "";
  const style = `position:absolute; height:${logo.height}px; ${filter}`;
  if (place === "top") return [`<img class="logo" data-card-logo src="${b.url(logo[lt])}" alt="" style="${style} left:${GUTTER}px; top:${GUTTER}px">`, 0];
  const top = H - GUTTER - logo.height;
  let html = `<img class="logo" data-card-logo src="${b.url(logo[lt])}" alt="" style="${style} left:${GUTTER}px; top:${top}px">`;
  // A hairline across the card above the signature, as the brand's own footer draws it.
  if (b.signature_rule) html += `<div style="position:absolute; left:0; right:0; top:${top - 40}px; height:2px; background:${b.signature_rule}"></div>`;
  if (b.address) {
    const a = b.address;
    const colour = a[theme] || a[lt];
    html += `<div data-card-logo style="position:absolute; right:${GUTTER}px; top:${top}px; height:${logo.height}px; display:flex; align-items:center; `
      + `${a.style}; color:${colour}">${escape(a.text)}</div>`;
  }
  return [html, logo.height + GUTTER + (b.signature_rule ? 88 : 48)];
}

function fill(b, theme) {
  if (theme === "art" || theme === "art-light") {
    if (!b.artwork) throw new ComposeError("The profile has no artwork, so no ground or band can be art.");
    // Single quotes: this goes inside a style attribute.
    return `background-image:url('${b.url(b.artwork[theme === "art" ? "dark" : "light"])}'); background-size:cover; background-position:center`;
  }
  if (!b.grounds[theme]) throw new ComposeError(`The profile has no ${theme} ground. Its grounds: ${Object.keys(b.grounds).join(", ")}, art, art-light.`);
  return `background:${b.grounds[theme]}`;
}

// The brand's own edge between the surface and a band, drawn by a class from its stylesheet at
// twice its size, when it has one for that band. y is the band's edge; a band above flips it.
function edge(b, theme, y, flip = false) {
  const e = b.edge;
  if (!e || !(e.classes || {})[theme]) return ["", 0];
  const [top, scale] = flip ? [y + e.height, "scale(2,-2)"] : [y - e.height, "scale(2)"];
  return [`<div class="${e.classes[theme]}" style="position:absolute; left:0; top:${top}px; width:${W / 2}px; height:${e.height / 2}px; `
    + `transform:${scale}; transform-origin:0 0"></div>`, e.height];
}

function window(b, post, left, top, w, h, onArt) {
  const frame = b.window;
  const theme = post.window_theme || "light";
  const ringWidth = frame.ring_width || 1;
  const ring = frame.ring ? `border:${ringWidth}px solid ${frame.ring[theme]};` : "";
  const radius = frame.radius || Math.round((post.radius || 0) * post.scale);
  const shadow = frame.shadow && onArt ? `box-shadow:${frame.shadow};` : "";
  const inset = ring ? 2 * ringWidth : 0;
  return `<div class="shot" data-card-screen style="position:absolute; left:${left}px; top:${top}px; width:${w}px; height:${h}px; `
    + `overflow:hidden; ${ring} border-radius:${radius}px; ${shadow}">`
    + `<img src="${post.shotUrl}" alt="" style="position:absolute; left:0; top:0; display:block; width:${w - inset}px"></div>`;
}

// The window's size on the card: the shot at its own size, enlarged by the post's one scale.
function size(post) {
  const {width, height} = pngSize(post.shotFile);
  const density = post.density || 2;
  return [Math.round(width / density * post.scale), Math.round(height / density * post.scale)];
}

const isArt = theme => theme === "art" || theme === "art-light";

// L02 Centred window: the headline at the top, the window centred under it, closed, on the
// surface or on a band of artwork or another ground.
function l02(b, post) {
  const {surface, band} = post;
  const [sig, foot] = b.logo.place === "foot" ? signature(b, band || surface, "foot") : signature(b, surface, "top");
  const headTop = b.logo.place === "top" ? GUTTER + 88 : 120;
  const under = headTop + headlineHeight(b);
  const [w, h] = size(post);
  let body = headline(b, post, surface, headTop);
  let roomTop = under + GUTTER;
  if (band) {
    const [edgeHtml, edgeHeight] = edge(b, band, under + GUTTER + ((b.edge || {}).height || 0));
    const bandTop = under + GUTTER + edgeHeight;
    body += `${edgeHtml}<div style="position:absolute; left:0; right:0; top:${bandTop}px; bottom:0; ${fill(b, band)}"></div>`;
    roomTop = bandTop;
  }
  const top = roomTop + Math.floor((H - foot - roomTop - h) / 2);
  return body + window(b, post, Math.floor((W - w) / 2), top, w, h, isArt(band)) + sig;
}

// L03 Window above the headline: a whole window centred near the top, the headline under it on
// the window's left edge, the logo at the foot. A band, when there is one, sits behind the
// window's upper part and ends under it with the brand's edge.
function l03(b, post) {
  const {surface, band} = post;
  const [sig, foot] = signature(b, surface, "foot");
  const [w, h] = size(post);
  // The window and its headline are one stack, centred in the space above the signature.
  const stack = h + 64 + headlineHeight(b);
  const left = Math.floor((W - w) / 2);
  const top = Math.max(96, GUTTER + Math.floor((H - foot - GUTTER - stack) / 2));
  let body = "";
  if (band) {
    const bandBottom = top + Math.round(h * 0.55);
    body += `<div style="position:absolute; left:0; right:0; top:0; height:${bandBottom}px; ${fill(b, band)}"></div>`;
    body += edge(b, band, bandBottom, true)[0];
  }
  body += window(b, post, left, top, w, h, isArt(band));
  body += headline(b, post, surface, top + h + 64, {left, width: W - left - GUTTER});
  return body + sig;
}

// L11 Centred stack: a centred headline over one whole window, the signature at the foot.
function l11(b, post) {
  const {surface, band} = post;
  const art = isArt(band);
  // A signature over artwork can't be read: artwork stops above it, and it sits on the surface.
  const [sig, foot] = signature(b, art || !band ? surface : band, "foot");
  const headTop = 112;
  const under = headTop + headlineHeight(b);
  const [w, h] = size(post);
  let body = headline(b, post, surface, headTop, {align: "center"});
  let roomTop = under + 56;
  if (band) {
    const [edgeHtml, edgeHeight] = edge(b, band, under + 48 + ((b.edge || {}).height || 0));
    const bandTop = under + 48 + edgeHeight;
    body += `${edgeHtml}<div style="position:absolute; left:0; right:0; top:${bandTop}px; bottom:${art ? foot : 0}px; ${fill(b, band)}"></div>`;
    roomTop = bandTop;
  }
  const roomBottom = H - foot - (art ? 24 : 0);
  const top = roomTop + Math.floor((roomBottom - roomTop - h) / 2);
  return body + window(b, post, Math.floor((W - w) / 2), top, w, h, art) + sig;
}

export const LAYOUTS = {L02: l02, L03: l03, L11: l11};

function readJSON(file, what) {
  if (!fs.existsSync(file)) throw new ComposeError(`${what} ${file} is missing.`);
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    throw new ComposeError(`${what} ${file} isn't valid JSON: ${error.message}`);
  }
}

// Write <dir>/<post>.html for every post in the batch and <dir>/cards.json for `tin-cards check`.
// Paths in the profile are relative to the profile, and paths in the batch to the batch; the cards
// refer to them relative to <dir>, so they render where they are.
export function compose(batchFile, dir) {
  const batch = readJSON(batchFile, "The batch");
  if (!batch.profile) throw new ComposeError("The batch names no profile: give the path to the brand's profile.");
  const profileFile = path.resolve(path.dirname(batchFile), batch.profile);
  const profile = readJSON(profileFile, "The profile");
  if (!profile.palette || !Object.keys(profile.palette).length) throw new ComposeError("The profile has no palette.");
  fs.mkdirSync(dir, {recursive: true});
  const relative = (base, ref) => (/^https?:\/\//.test(ref) ? ref : path.relative(dir, path.resolve(base, ref)).split(path.sep).join("/"));
  const b = {...profile, url: ref => relative(path.dirname(profileFile), ref)};
  const cards = [];
  for (const entry of batch.posts || []) {
    const layout = LAYOUTS[entry.layout];
    if (!layout) throw new ComposeError(`${entry.id}: no layout ${entry.layout}. The kit composes ${Object.keys(LAYOUTS).join(", ")}.`);
    if (!entry.scale) throw new ComposeError(`${entry.id}: give the scale the shot shows at, the --card-scale it was shot with.`);
    const shotFile = path.resolve(path.dirname(batchFile), entry.shot);
    if (!fs.existsSync(shotFile)) throw new ComposeError(`${entry.id}: the shot ${shotFile} is missing.`);
    const post = {...entry, shotFile, shotUrl: relative(path.dirname(batchFile), entry.shot)};
    const html = `<!doctype html><html><head>${head(b)}</head><body><main data-card style="${fill(b, post.surface)}">${layout(b, post)}</main></body></html>`;
    fs.writeFileSync(path.join(dir, `${post.id}.html`), html);
    cards.push({post: post.id, line: [post.lit, post.turn].filter(Boolean).join(" "), alt_text: post.alt || ""});
  }
  if (!cards.length) throw new ComposeError("The batch lists no posts.");
  fs.writeFileSync(path.join(dir, "cards.json"), `${JSON.stringify({palette: profile.palette, cards}, null, 1)}\n`);
  return cards;
}
