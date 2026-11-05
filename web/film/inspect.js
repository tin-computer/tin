// What a live page offers a film, read at the film's own screen size: its headings and the
// controls a hand could press, each with a selector that matches it alone, where it sits, and two
// screenshots. The page is opened through the film's recording (recording.js) in record mode, so
// looking is already recording, under the same rules: trackers get an empty answer, and nothing
// that sends, publishes or pays leaves the browser.
import fs from "node:fs";
import path from "node:path";
import {chromium} from "playwright";
import {FORMATS, stage} from "./film.js";
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
  const body = getComputedStyle(document.body);
  return {
    title: document.title, url: location.href, height: document.documentElement.scrollHeight,
    look: {background: body.backgroundColor, color: body.color, font: body.fontFamily.split(",")[0].trim()},
    headings, controls,
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
    const found = await page.evaluate(outline);
    await page.screenshot({path: path.join(dir, "screen.png")});
    await page.screenshot({path: path.join(dir, "page.png"), fullPage: true, clip: {x: 0, y: 0, width: screen.width, height: Math.min(found.height, 6000)}});
    const report = {...found, format, screen, recording: recording.close()};
    fs.writeFileSync(path.join(dir, "inspect.json"), `${JSON.stringify(report, null, 1)}\n`);
    const spec = path.join(dir, "film.json");
    if (!fs.existsSync(spec)) fs.writeFileSync(spec, `${JSON.stringify(starter({url: found.url, title: found.title, format}), null, 2)}\n`);
    return report;
  } finally {
    await browser.close();
  }
}
