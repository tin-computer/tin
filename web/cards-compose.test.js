// The card kit's composer on a synthetic brand: a profile, a batch and a shot written to a
// temporary folder, composed into cards whose layout, paths and words are read back.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {ComposeError, LAYOUTS, compose} from "./cards/compose.js";

// Only the header matters to the composer: the PNG signature and the IHDR's width and height.
function png(width, height) {
  const bytes = Buffer.alloc(33);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(bytes, 0);
  bytes.writeUInt32BE(13, 8);
  bytes.write("IHDR", 12, "latin1");
  bytes.writeUInt32BE(width, 16);
  bytes.writeUInt32BE(height, 20);
  return bytes;
}

const PROFILE = {
  palette: {paper: "#f7f5ef", ink: "#26231c", night: "#141210"},
  fonts: [{family: "Example Sans", src: "brand/example-sans.woff2", weight: 400}],
  grounds: {light: "#f7f5ef", dark: "#141210"},
  headline: {
    family: "'Example Sans', sans-serif", weight: 400, size: 76, line: 1.04, track: "-0.02em",
    light: {lit: "#26231c", turn: "#8a8375"},
    dark: {lit: "#f6f1e7", turn: "#857c6f"},
  },
  logo: {place: "top", height: 40, light: "brand/logo-ink.png", dark: "brand/logo-paper.png"},
  artwork: {dark: "brand/painting dark.webp", light: "brand/painting light.webp"},
  window: {ring: {light: "#26231c", dark: "#f6f1e7"}, radius: 14, shadow: "0 24px 60px #000"},
};

// A brand folder, a batch folder beside it and an output folder elsewhere, so every path the
// cards hold has to be worked out relative to where the cards are.
function setUp(posts, profile = PROFILE) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "tin-cards-compose-"));
  fs.mkdirSync(path.join(root, "company/brand"), {recursive: true});
  fs.mkdirSync(path.join(root, "company/posts/shots"), {recursive: true});
  fs.writeFileSync(path.join(root, "company/profile.json"), JSON.stringify(profile));
  fs.writeFileSync(path.join(root, "company/posts/shots/analytics.png"), png(800, 900));
  const batch = path.join(root, "company/posts/batch.json");
  fs.writeFileSync(batch, JSON.stringify({profile: "../profile.json", posts}));
  return {batch, out: path.join(root, "out/cards"), root};
}

const post = (extra = {}) => ({id: "p1", layout: "L02", surface: "dark", lit: "See visits", turn: "and automation apart", shot: "shots/analytics.png", scale: 1.5, alt: "The Analytics page with a week's chart. Headline: See visits and automation apart.", ...extra});

const windowBox = html => {
  const match = html.match(/data-card-screen style="position:absolute; left:(\d+)px; top:(\d+)px; width:(\d+)px; height:(\d+)px/);
  return match && match.slice(1).map(Number);
};

test("each layout puts the window at the shot's size times the post's scale, centred across the card", () => {
  for (const layout of Object.keys(LAYOUTS)) {
    const {batch, out} = setUp([post({layout})]);
    compose(batch, out);
    const html = fs.readFileSync(path.join(out, "p1.html"), "utf8");
    // 800 x 900 shot at twice its size, shown 1.5 times larger: 600 x 675 on the card.
    const [left, top, width, height] = windowBox(html);
    assert.deepEqual([width, height], [600, 675], layout);
    assert.equal(left, (1080 - 600) / 2, layout);
    assert.ok(top > 0 && top + height < 1350, `${layout}: the window sits inside the card`);
    assert.equal((html.match(/<main data-card/g) || []).length, 1, layout);
  }
});

test("cards.json carries the brand's palette and each post's line and alt text", () => {
  const {batch, out} = setUp([post(), post({id: "p2", layout: "L11", turn: undefined, lit: "Read the answer"})]);
  const cards = compose(batch, out);
  const listed = JSON.parse(fs.readFileSync(path.join(out, "cards.json"), "utf8"));
  assert.deepEqual(listed.palette, PROFILE.palette);
  assert.deepEqual(listed.cards.map(card => [card.post, card.line]), [["p1", "See visits and automation apart"], ["p2", "Read the answer"]]);
  assert.deepEqual(cards, listed.cards);
  assert.match(listed.cards[0].alt_text, /Headline: See visits/);
});

test("the brand's files and the shot are named relative to the cards, so they load where the cards are", () => {
  const {batch, out, root} = setUp([post({band: "art"})]);
  compose(batch, out);
  const html = fs.readFileSync(path.join(out, "p1.html"), "utf8");
  const resolve = ref => path.resolve(out, ref);
  const logo = html.match(/data-card-logo src="([^"]+)"/)[1];
  assert.equal(resolve(logo), path.join(root, "company/brand/logo-paper.png"));
  const shot = html.match(/<img src="([^"]+)"/)[1];
  assert.equal(resolve(shot), path.join(root, "company/posts/shots/analytics.png"));
  const font = html.match(/@font-face\{[^}]*url\("([^"]+)"\)/)[1];
  assert.equal(resolve(font), path.join(root, "company/brand/example-sans.woff2"));
});

test("artwork sits whole inside its style attribute, a space in its name included", () => {
  const {batch, out, root} = setUp([post({band: "art"})]);
  compose(batch, out);
  const html = fs.readFileSync(path.join(out, "p1.html"), "utf8");
  // A double quote here would end the attribute, and the band would render without its painting.
  const style = html.match(/<div style="([^"]*background-image[^"]*)"/);
  assert.ok(style, "the band's style holds its background image");
  const art = style[1].match(/url\('([^']+)'\)/)[1];
  assert.equal(path.resolve(out, art), path.join(root, "company/brand/painting dark.webp"));
});

test("the headline's words are escaped, and its turn takes the ground's colour", () => {
  const {batch, out} = setUp([post({surface: "light", lit: "Plans & results", turn: "<in one place>"})]);
  compose(batch, out);
  const html = fs.readFileSync(path.join(out, "p1.html"), "utf8");
  assert.match(html, /data-card-line[^>]*><span style="color:#26231c">Plans &amp; results<\/span> <span style="color:#8a8375; font-style:normal">&lt;in one place&gt;<\/span><\/h1>/);
});

test("a batch the composer can't lay out says what to change", () => {
  const cases = [
    [[post({layout: "L07"})], /no layout L07\. The kit composes L02, L03, L11/],
    [[post({shot: "shots/missing.png"})], /p1: the shot .*missing\.png is missing/],
    [[post({surface: "accent"})], /no accent ground\. Its grounds: light, dark, art, art-light/],
    [[post({scale: undefined})], /give the scale the shot shows at/],
    [[], /lists no posts/],
  ];
  for (const [posts, message] of cases) {
    const {batch, out} = setUp(posts);
    assert.throws(() => compose(batch, out), error => error instanceof ComposeError && message.test(error.message));
  }
  const {batch, out} = setUp([post({band: "art"})], {...PROFILE, artwork: null});
  assert.throws(() => compose(batch, out), /has no artwork/);
});
