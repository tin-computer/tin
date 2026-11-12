#!/usr/bin/env node
// Tin's card kit: render each LinkedIn post's card from its HTML, check it, and lay the batch out
// on one sheet to look at before uploading.
//
//   tin-cards check <dir>               render <dir>/<post>.html to <post>.png and check every card
//   tin-cards shoot <page> <out.png>    shoot a product page, or one part of it, for a card's screen
//   tin-cards guide                     print the guide
//
// <dir>/cards.json holds the brand's palette and lists the cards:
//   {"palette": {"paper": "#f7f5ef", ...}, "cards": [{"post": "p1", "line": "...", "alt_text": "..."}]}
// Each card is <dir>/<post>.html, one <main data-card> at 1080 x 1350 (see guide/README.md).

import fs from "node:fs";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {chromium} from "playwright";
import {HEIGHT, WIDTH, colours, emptyWindows, eyebrows, fonts, leftBars, placement, textSizes, words} from "./checks.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// LinkedIn takes PNG or JPEG up to 5 MB, and so does Tin.
const MAX_BYTES = 5 * 1024 * 1024;
const MAX_LINE_WORDS = 9;
// The smallest text a card carries, in card pixels: 2% of its width, as for films, so a screen's
// words still read when the card shows at about half size in the feed.
const SMALLEST_TEXT = Math.round(0.02 * WIDTH);

function fail(message) {
  console.error(message);
  process.exit(1);
}

function normalise(text) {
  return (text || "").replace(/[’‘]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
}

// Each colour as [r, g, b, a] in sRGB, whatever syntax it was written in (hex, rgb(), oklch()),
// read back from a one-pixel canvas.
function inPageRGBA(values) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const context = canvas.getContext("2d", {willReadFrequently: true});
  return values.map(value => {
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = "#000";
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  });
}

// A colour is the brand's when its red, green and blue match a palette colour; its opacity is
// free, so a token at 12% is still that token. Low opacities read back less exactly, so they get
// more room.
function inPalette([r, g, b, a], palette) {
  const room = 3 + Math.ceil(1 / Math.max(a, 0.02));
  return palette.some(([pr, pg, pb]) => Math.abs(pr - r) <= room && Math.abs(pg - g) <= room && Math.abs(pb - b) <= room);
}

const hex = ([r, g, b, a]) => `#${[r, g, b].map(v => v.toString(16).padStart(2, "0")).join("")}${a < 0.99 ? ` at ${Math.round(a * 100)}%` : ""}`;

async function settle(page) {
  await page.evaluate(async () => {
    // A lazy image below the fold never loads by itself: load every image now.
    for (const image of document.images) image.loading = "eager";
    await document.fonts.ready;
    const images = Promise.all([...document.images].map(image => image.complete ? null : new Promise(done => {
      image.addEventListener("load", done, {once: true});
      image.addEventListener("error", done, {once: true});
    })));
    await Promise.race([images, new Promise(done => setTimeout(done, 10000))]);
  });
}

async function checkCard(page, dir, card, palette) {
  const errors = [];
  const warnings = [];
  const file = path.join(dir, `${card.post}.html`);
  if (!fs.existsSync(file)) return {errors: [`${card.post}: ${card.post}.html is missing.`], warnings};
  await page.goto(pathToFileURL(file).href, {waitUntil: "load"});
  await settle(page);
  const seen = await page.evaluate(words);
  if (seen.missing) return {errors: [`${card.post}: the page has no <main data-card>.`], warnings};
  if (seen.size.width !== WIDTH || seen.size.height !== HEIGHT) {
    errors.push(`${card.post}: the card is ${seen.size.width} x ${seen.size.height}; make it ${WIDTH} x ${HEIGHT}.`);
  }
  for (const face of await page.evaluate(fonts)) {
    if (face.generic) errors.push(`${card.post}: “${face.sample}” is set in ${face.family}, a fallback. Load the company's own font.`);
    else if (!face.loaded) errors.push(`${card.post}: the font ${face.family} didn't load, so “${face.sample}” falls back. Check its @font-face.`);
  }
  if (!seen.headline) errors.push(`${card.post}: mark the headline with data-card-line.`);
  const stray = seen.items.filter(item => !item.inScreen && !item.inLogo && !item.line);
  for (const item of stray) {
    errors.push(`${card.post}: “${item.text}” is on the card outside its headline. A card carries its headline and nothing else; put product words inside data-card-screen.`);
  }
  for (const item of seen.items.filter(item => item.outside || item.clipped)) {
    const where = item.inScreen ? "in the screen" : "in the headline";
    const message = `${card.post}: “${item.text}” ${item.outside ? "runs past the card's edge" : "is clipped"} ${where}.`;
    // A screen may run off the card's edge by design; the headline never may.
    if (item.inScreen) warnings.push(message);
    else errors.push(message);
  }
  const painted = await page.evaluate(colours);
  const values = await page.evaluate(inPageRGBA, painted.map(item => item.value));
  painted.forEach((item, index) => {
    if (inPalette(values[index], palette)) return;
    errors.push(`${card.post}: ${hex(values[index])}, the ${item.what} of “${item.sample}”, isn't one of the brand's colours. Use a colour from the palette in cards.json.`);
  });
  for (const item of await page.evaluate(leftBars, {})) {
    errors.push(`${card.post}: “${item.text}” has a bar down its left edge. Tin marks nothing that way: drop the border, or give the box the same border all round.`);
  }
  for (const item of await page.evaluate(placement)) {
    if (item.satellite || item.left <= 1 || item.right <= 1 || Math.abs(item.left - item.right) <= 4) continue;
    errors.push(`${card.post}: the screen “${item.sample}” sits ${item.left} px from the left of ${item.frame} and ${item.right} px from the right. Centre it, or inset it a gutter from the left and run it off the right edge.`);
  }
  for (const item of await page.evaluate(emptyWindows)) {
    errors.push(`${card.post}: the window around ${item.sample} is empty for ${item.open.join(" and ")}. Size the window to its screen, and stage the screen so its own content runs off the edge.`);
  }
  for (const item of await page.evaluate(eyebrows)) {
    errors.push(`${card.post}: an eyebrow, “${item.text}”, sits small above “${item.under}”. Tin never uses eyebrows: delete it.`);
  }
  if (seen.headline && seen.headline.split(" ").length > MAX_LINE_WORDS) {
    errors.push(`${card.post}: the headline has ${seen.headline.split(" ").length} words; keep it to ${MAX_LINE_WORDS}.`);
  }
  if (card.line && seen.headline && normalise(card.line) !== normalise(seen.headline)) {
    warnings.push(`${card.post}: the headline reads “${seen.headline}”, and the draft's card line is “${card.line}”. Say which in the upload.`);
  }
  if (!card.alt_text || card.alt_text.trim().length < 20) {
    errors.push(`${card.post}: write alt text in cards.json that describes the card and says its headline.`);
  }
  let image = path.join(dir, `${card.post}.png`);
  await page.screenshot({path: image, clip: {x: 0, y: 0, width: WIDTH, height: HEIGHT}});
  if (fs.statSync(image).size > MAX_BYTES) {
    fs.rmSync(image);
    image = path.join(dir, `${card.post}.jpg`);
    await page.screenshot({path: image, type: "jpeg", quality: 90, clip: {x: 0, y: 0, width: WIDTH, height: HEIGHT}});
    if (fs.statSync(image).size > MAX_BYTES) errors.push(`${card.post}: the image is over 5 MB even as a JPEG.`);
  }
  return {errors, warnings, image};
}

async function sheet(browser, dir, images) {
  const page = await browser.newPage({viewport: {width: 1800, height: 1200}});
  const cells = images.map(image => `<figure><img src="${pathToFileURL(image).href}"><figcaption>${path.basename(image)}</figcaption></figure>`).join("");
  // Written beside the cards and opened from there, so the page may load them.
  const html = path.join(dir, "sheet.html");
  fs.writeFileSync(html, `<!doctype html><style>body{margin:0;padding:40px;background:#e9e6df;font:16px system-ui;display:grid;grid-template-columns:repeat(${Math.min(images.length, 3)},1fr);gap:24px}img{width:100%;display:block}figcaption{margin-top:8px;color:#555}</style>${cells}`);
  await page.goto(pathToFileURL(html).href, {waitUntil: "load"});
  await settle(page);
  const out = path.join(dir, "sheet.png");
  await page.screenshot({path: out, fullPage: true});
  fs.rmSync(html);
  await page.close();
  return out;
}

async function check(dir) {
  if (!dir || !fs.existsSync(dir)) fail("Usage: tin-cards check <dir>, the folder with cards.json and the cards' HTML.");
  const listed = path.join(dir, "cards.json");
  if (!fs.existsSync(listed)) fail(`${listed} is missing: list each card's post, line and alt_text.`);
  const {palette: named, cards} = JSON.parse(fs.readFileSync(listed, "utf8"));
  if (!named || !Object.keys(named).length) fail("cards.json has no palette. Copy the brand's colours from brand/BRAND.md (Tin gives them with the draft).");
  if (!Array.isArray(cards) || !cards.length) fail("cards.json lists no cards.");
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: WIDTH, height: HEIGHT}, deviceScaleFactor: 1});
  await page.setContent("<!doctype html><title>palette</title>");
  const palette = await page.evaluate(inPageRGBA, Object.values(named));
  const images = [];
  let errors = 0;
  for (const card of cards) {
    const result = await checkCard(page, dir, card, palette);
    for (const message of result.errors) console.error(`error  ${message}`);
    for (const message of result.warnings) console.error(`note   ${message}`);
    errors += result.errors.length;
    if (result.image) images.push(result.image);
  }
  const out = images.length ? await sheet(browser, dir, images) : null;
  await browser.close();
  if (out) console.log(`Look at ${out} before uploading.`);
  if (errors) fail(`${errors} problem${errors === 1 ? "" : "s"} to fix.`);
  console.log(`${cards.length} card${cards.length === 1 ? "" : "s"} passed.`);
}

// A product page, shot as the product draws it, for a card's screen: the company's own markup,
// stylesheet and fonts with sample data, from a local file or the running app. Shot at twice
// the size so it stays sharp when the card scales it.
async function shoot(source, out, options) {
  if (!source || !out) fail("Usage: tin-cards shoot <page.html | http://localhost:…> <out.png> [--width 1280] [--height 900] [--selector css] [--scale 2] [--scheme dark] [--wait css] [--style simplify.css] [--script simplify.js] [--card-scale 1.5]");
  // A local page may carry a query (page.html?view=analytics) that picks what it shows.
  const [file, query] = source.split("?");
  const url = /^https?:\/\//.test(source) ? source : `${pathToFileURL(path.resolve(file)).href}${query ? `?${query}` : ""}`;
  const width = Number(options.width || 1280);
  const height = Number(options.height || 900);
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width, height}, deviceScaleFactor: Number(options.scale || 2), colorScheme: options.scheme === "dark" ? "dark" : "light"});
  await page.goto(url, {waitUntil: "load"});
  // A live site may never go quiet (analytics keep polling): wait a little for its data, then shoot.
  await page.waitForLoadState("networkidle", {timeout: 5000}).catch(() => {});
  // A page that draws its data after loading: wait for the part the shot needs.
  if (options.wait) await page.waitForSelector(options.wait, {timeout: 15000}).catch(() => fail(`Nothing matching ${options.wait} appeared on the page.`));
  await settle(page);
  // Simplify the page for the card: hide what the post isn't about, put quiet bars where words
  // nobody needs to read were.
  if (options.style) await page.addStyleTag({path: path.resolve(options.style)});
  if (options.script) {
    await page.addScriptTag({path: path.resolve(options.script)});
    await settle(page);
  }
  let problems = 0;
  for (const face of await page.evaluate(fonts, "body")) {
    if (!face.generic && face.loaded) continue;
    console.error(`error  “${face.sample}” is set in ${face.family}, ${face.generic ? "a fallback" : "which didn't load"}. Load the product's own font before shooting it.`);
    problems += 1;
  }
  const target = options.selector ? page.locator(options.selector).first() : page;
  if (options.selector && !(await target.count())) fail(`Nothing on the page matches ${options.selector}.`);
  const region = options.selector ? await target.boundingBox() : {x: 0, y: 0, width, height};
  // A product may mark a quote or a note with a bar down its left edge; a card never shows one.
  for (const item of await page.evaluate(leftBars, {scope: "body", region})) {
    console.error(`error  “${item.text}” has a bar down its left edge. Drop the border in the stage script, or give the box the same border all round.`);
    problems += 1;
  }
  if (options["card-scale"]) {
    // How much larger the shot shows on the card: every word in it must reach the card's floor.
    const scale = Number(options["card-scale"]);
    const small = (await page.evaluate(textSizes, region)).filter(item => item.size * scale < SMALLEST_TEXT);
    for (const item of small.slice(0, 8)) {
      console.error(`error  “${item.text}” is ${item.size} px, ${Math.round(item.size * scale)} px on the card; the smallest a card carries is ${SMALLEST_TEXT}. Hide it, put a bar in its place, or enlarge the shot.`);
    }
    problems += small.length;
  }
  await target.screenshot({path: out, animations: "disabled", timeout: 30000});
  await browser.close();
  if (problems) fail(`${problems} problem${problems === 1 ? "" : "s"}: the shot is at ${out}, but don't use it until they're fixed.`);
  console.log(`Shot ${url}${options.selector ? ` (${options.selector})` : ""} to ${out}.`);
}

function flags(args) {
  const options = {};
  const rest = [];
  for (let i = 0; i < args.length; i += 1) {
    if (args[i].startsWith("--")) options[args[i].slice(2)] = args[(i += 1)];
    else rest.push(args[i]);
  }
  return {options, rest};
}

const [command, ...args] = process.argv.slice(2);
if (command === "check") await check(args[0]);
else if (command === "shoot") {
  const {options, rest} = flags(args);
  await shoot(rest[0], rest[1], options);
} else if (command === "guide") console.log(fs.readFileSync(path.join(HERE, "guide", "README.md"), "utf8"));
else fail("Usage: tin-cards check <dir> | tin-cards shoot <page> <out.png> | tin-cards guide");
