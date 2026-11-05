// What a live page offers a film, read at the film's own screen size: its look as the stage kit's
// tokens, its headings and the controls a hand could press, each with a selector that matches it
// alone, where it sits, and two screenshots. The page is opened through the film's recording (recording.js) in record mode, so
// looking is already recording, under the same rules: trackers get an empty answer, and nothing
// that sends, publishes or pays leaves the browser.
import fs from "node:fs";
import path from "node:path";
import {chromium} from "playwright";
import {FORMATS, encodeStills, stage} from "./film.js";
import {openRecording} from "./recording.js";

// Runs in the page: the outline, with a selector for each entry that matches only it.
function outline() {
  const ok = (selector, element) => {
    try {
      const found = document.querySelectorAll(selector);
      return found.length === 1 && found[0] === element;
    } catch {
      return false;
    }
  };
  const quote = value => `"${value.replace(/["\\]/g, "\\$&")}"`;
  const unique = element => {
    if (element.id && ok(`#${CSS.escape(element.id)}`, element)) return `#${CSS.escape(element.id)}`;
    for (const name of ["data-testid", "data-test", "aria-label", "name", "href", "placeholder", "title"]) {
      const value = element.getAttribute(name);
      const selector = value && value.length < 120 && `${element.localName}[${name}=${quote(value)}]`;
      if (selector && ok(selector, element)) return selector;
    }
    // A path of positions, from the nearest ancestor that has an id of its own.
    const parts = [];
    for (let node = element; node && node !== document.documentElement; node = node.parentElement) {
      if (node !== element && node.id && ok(`#${CSS.escape(node.id)}`, node)) {
        parts.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const same = node.parentElement ? [...node.parentElement.children].filter(child => child.localName === node.localName) : [];
      parts.unshift(same.length > 1 ? `${node.localName}:nth-of-type(${same.indexOf(node) + 1})` : node.localName);
      if (ok(parts.join(" > "), element)) break;
    }
    return parts.join(" > ");
  };
  const words = element => (element.innerText || element.getAttribute("aria-label") || element.getAttribute("placeholder") || element.getAttribute("title") || element.value || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const shown = element => {
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return box.width > 2 && box.height > 2 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0;
  };
  const place = element => {
    const box = element.getBoundingClientRect();
    return {top: Math.round(box.top + scrollY), left: Math.round(box.left + scrollX), width: Math.round(box.width), height: Math.round(box.height)};
  };
  const headings = [...document.querySelectorAll("h1, h2, h3")].filter(shown).slice(0, 60)
    .map(element => ({level: Number(element.localName[1]), text: words(element), selector: unique(element), ...place(element)}));
  const controls = [...document.querySelectorAll("a[href], button, [role=button], [role=tab], input:not([type=hidden]), textarea, select, summary")]
    .filter(shown).slice(0, 150)
    .map(element => ({kind: element.localName === "a" ? "link" : element.localName, text: words(element), href: element.getAttribute("href") || undefined, selector: unique(element), ...place(element)}));
  return {
    title: document.title, url: location.href, height: document.documentElement.scrollHeight,
    look: look(shown), headings, controls,
  };
}

// Runs in the page: its look as the stage kit's tokens (stage/stage.css), read from what it
// draws: the ground and text, a card, the main button, a muted paragraph, its type, and the
// stylesheets that bring its fonts.
function look(shown) {
  const style = element => element && getComputedStyle(element);
  const filled = value => value && value !== "transparent" && !/rgba\([^)]*,\s*0\)$/.test(value);
  const body = style(document.body);
  const ground = filled(body.backgroundColor) ? body.backgroundColor : style(document.documentElement).backgroundColor;
  const first = (selector, test = () => true) => [...document.querySelectorAll(selector)].find(element => shown(element) && test(element, style(element)));
  const button = first("button, a, [role=button]", (element, s) => filled(s.backgroundColor) && s.backgroundColor !== ground && element.getBoundingClientRect().height < 80);
  const card = first("article, section, li, div", (element, s) => {
    const box = element.getBoundingClientRect();
    return box.width > 160 && box.height > 60 && box.width < innerWidth * 0.9 && filled(s.backgroundColor) && s.backgroundColor !== ground && parseFloat(s.borderRadius) > 0;
  });
  const heading = first("h1") || first("h2");
  const muted = first("p", (element, s) => s.color !== body.color);
  const code = first("code, pre, kbd");
  const link = first("a", (element, s) => s.color !== body.color && !filled(s.backgroundColor));
  const family = s => s?.fontFamily;
  const tokens = {
    "--ground": ground,
    "--surface": ground,
    "--card": style(card)?.backgroundColor || ground,
    "--ink": body.color,
    "--ink-2": style(muted)?.color || body.color,
    "--accent": style(link)?.color || style(button)?.backgroundColor || body.color,
    "--hot": style(button)?.backgroundColor || style(link)?.color || body.color,
    "--on-hot": style(button)?.color || ground,
    "--sans": family(body),
    "--mono": family(style(code)) || "ui-monospace, monospace",
    "--radius": style(card)?.borderRadius || style(button)?.borderRadius || "10px",
  };
  return {
    background: ground, color: body.color, font: body.fontFamily.split(",")[0].trim(),
    heading: heading && {font: style(heading).fontFamily.split(",")[0].trim(), weight: style(heading).fontWeight, letterSpacing: style(heading).letterSpacing},
    tokens: `:root { ${Object.entries(tokens).map(([name, value]) => `${name}: ${value};`).join(" ")} }`,
    stylesheets: [...document.querySelectorAll("link[rel=stylesheet]")].map(element => element.href).filter(href => /font/i.test(href)).slice(0, 4),
  };
}

// A film to start from: the page's title, its address, now as the page's clock, and beats that
// show the first screen and end.
export function starter({url, title, format}) {
  const now = new Date();
  now.setUTCSeconds(0, 0);
  return {
    title: title || new URL(url).hostname,
    story: {before: "", after: ""},
    site: {url},
    now: now.toISOString(),
    format,
    ready: "body",
    beats: [
      {camera: "body", cut: true, pad: 0},
      {caption: "One sentence the viewer reads first."},
      {begin: true},
      {hold: 2},
      {mark: "poster"},
      {end: true},
    ],
  };
}

export async function inspect(url, {dir, format = "16:9"} = {}) {
  const size = FORMATS[format];
  if (!size) throw new Error(`Unknown format ${format}; use ${Object.keys(FORMATS).join(", ")}.`);
  fs.mkdirSync(dir, {recursive: true});
  const {screen} = stage(size);
  const recording = openRecording(path.join(dir, "world"), {record: true});
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: screen, deviceScaleFactor: 1, locale: "en-US"});
    await context.route("**/*", route => recording.route(route));
    const page = await context.newPage();
    await page.goto(url, {waitUntil: "load", timeout: 60_000});
    await page.waitForLoadState("networkidle", {timeout: 10_000}).catch(() => {});
    await page.waitForTimeout(1000);
    const found = await page.evaluate(`(${outline.toString().replace("look(shown)", `(${look})(shown)`)})()`);
    // JPEGs a reader can afford to open: the first screen at up to 960 px wide, the page at 720.
    const shot = async (options, width, file) => encodeStills([await page.screenshot(options)], ["-vf", `scale='min(${width},iw)':-2:flags=lanczos`, "-q:v", "4", "-frames:v", "1", path.join(dir, file)]);
    await shot({}, 960, "screen.jpg");
    await shot({fullPage: true, clip: {x: 0, y: 0, width: screen.width, height: Math.min(found.height, 6000)}}, 720, "page.jpg");
    const report = {...found, format, screen, recording: recording.close()};
    fs.writeFileSync(path.join(dir, "inspect.json"), `${JSON.stringify(report, null, 1)}\n`);
    const spec = path.join(dir, "film.json");
    if (!fs.existsSync(spec)) fs.writeFileSync(spec, `${JSON.stringify(starter({url: found.url, title: found.title, format}), null, 2)}\n`);
    return report;
  } finally {
    await browser.close();
  }
}
