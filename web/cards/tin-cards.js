#!/usr/bin/env node
// Tin's card kit: render each LinkedIn post's card from its HTML, check it, and lay the batch out
// on one sheet to look at before uploading.
//
//   tin-cards check <dir>   render <dir>/<post>.html to <post>.png and check every card
//   tin-cards guide         print the guide
//
// <dir>/cards.json lists the cards: [{"post": "p1", "line": "...", "alt_text": "..."}]. Each
// card is <dir>/<post>.html, one <main data-card> at 1080 x 1350 (see guide/README.md).

import fs from "node:fs";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {chromium} from "playwright";
import {HEIGHT, WIDTH, eyebrows, fonts, words} from "./checks.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// LinkedIn takes PNG or JPEG up to 5 MB, and so does Tin.
const MAX_BYTES = 5 * 1024 * 1024;
const MAX_LINE_WORDS = 9;

function fail(message) {
  console.error(message);
  process.exit(1);
}

function normalise(text) {
  return (text || "").replace(/[’‘]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
}

async function settle(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(image => image.complete ? null : new Promise(done => {
      image.addEventListener("load", done, {once: true});
      image.addEventListener("error", done, {once: true});
    })));
  });
}

async function checkCard(page, dir, card) {
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
  const cards = JSON.parse(fs.readFileSync(listed, "utf8"));
  if (!Array.isArray(cards) || !cards.length) fail("cards.json lists no cards.");
  const browser = await chromium.launch();
  const page = await browser.newPage({viewport: {width: WIDTH, height: HEIGHT}, deviceScaleFactor: 1});
  const images = [];
  let errors = 0;
  for (const card of cards) {
    const result = await checkCard(page, dir, card);
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

const [command, arg] = process.argv.slice(2);
if (command === "check") await check(arg);
else if (command === "guide") console.log(fs.readFileSync(path.join(HERE, "guide", "README.md"), "utf8"));
else fail("Usage: tin-cards check <dir> | tin-cards guide");
