// tin-film: a film written as JSON plays a live site from its recording, says what is wrong with a
// spec before it reaches the screen, and becomes a note that plays the film from its own folder.
import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {filmFromSpec, shoot, vtt} from "./film/film.js";
import {recordingKey} from "./film/recording.js";
import {boardIssues, deliverDirections, directionsPacket, note, restore, reviewPacket, sketch, upload} from "./film/tin-film.js";
import {FAMILIES, STRUCTURES, deal} from "./film/directions.js";
import {faceName, fontFaces, inspect} from "./film/inspect.js";
import http from "node:http";

const SITE = "https://film.test/";
const PAGE = `<!doctype html><html><head><title>Fold</title><style>
  body { margin: 0; font: 18px/1.5 system-ui, sans-serif; background: #f4f1ea; color: #1c1b18; }
  main { padding: 48px 40px; } h1 { font-size: 40px; margin: 0 0 16px; }
  button { font: inherit; padding: 12px 20px; border-radius: 8px; border: 1px solid #1c1b18; background: #fff; }
</style></head><body><main><h1>Fold</h1><p>Every sheep, back in the fold.</p><button id="go">Herd them</button></main></body></html>`;

// A site's recording as tin-film record leaves it, beside a film.json.
function filmFolder(beats) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-"));
  const body = Buffer.from(PAGE);
  const hash = crypto.createHash("sha1").update(body).digest("hex");
  fs.mkdirSync(path.join(dir, "world", "bodies"), {recursive: true});
  fs.writeFileSync(path.join(dir, "world", "bodies", hash), body);
  const entry = {method: "GET", url: SITE, status: 200, headers: {"content-type": "text/html"}, body: hash, bytes: body.length};
  fs.writeFileSync(path.join(dir, "world", "index.json"), JSON.stringify({version: 1, entries: {[recordingKey("GET", SITE)]: entry}}));
  const file = path.join(dir, "film.json");
  const spec = {title: "Every sheep, back in the fold", story: {before: "Sheep wander.", after: "One press herds them."}, site: {url: SITE}, now: "2026-11-05T10:00:00Z", format: "4:5", theme: "light", ready: "h1", beats};
  fs.writeFileSync(file, JSON.stringify(spec));
  return {dir, file, spec};
}

const BEATS = [
  {camera: "main", cut: true},
  {caption: "One press herds every sheep."},
  {begin: true},
  {hold: 0.6},
  {click: "#go"},
  {hold: 0.4},
  {mark: "poster"},
  {end: true},
];

test("a film written as JSON plays its site from the recording beside it", {timeout: 180000}, async () => {
  const {dir, file, spec} = filmFolder(BEATS);
  const film = filmFromSpec(spec, file);
  // CI has no ffmpeg, so this take is played and checked without an encoder, as film.browser does.
  const {report} = await shoot(film, {format: "4:5", out: path.join(dir, "takes"), rehearse: true, check: true, encode: false});
  assert.deepEqual(report.issues.errors, []);
  assert.equal(report.recording.misses.length, 0);
  assert.equal(report.recording.added, 0, "a rehearsal never goes to the live site");
  assert.deepEqual(report.captions.map(item => item.text), ["One press herds every sheep."]);
  assert.ok(fs.existsSync(path.join(dir, "takes", "film", "4x5-light", "report.json")));
});

test("a spec whose click hits nothing stops at that beat", {timeout: 180000}, async () => {
  const {dir, file, spec} = filmFolder(BEATS.map(beat => (beat.click ? {click: "#send-invoices"} : beat)));
  await assert.rejects(shoot(filmFromSpec(spec, file), {format: "4:5", out: path.join(dir, "takes"), rehearse: true, encode: false}), /Nothing on screen matches "#send-invoices"/);
});

test("a spec missing what a film needs is refused before anything is filmed", () => {
  assert.throws(() => filmFromSpec({title: "", site: {url: "http://film.test/"}, beats: []}, "film.json"),
    /film.json needs a title, a stage \(the page the film plays, beside film.json\) or site.url, the https address it opens on, now, the moment the page's clock shows, as an ISO time, beats\./);
  assert.throws(() => filmFromSpec({...filmFolder(BEATS).spec, site: undefined, stage: "../elsewhere.html"}, "film.json"), /stage, a page beside film.json/);
  assert.throws(() => filmFromSpec({...filmFolder(BEATS).spec, beats: [{tap: "#go"}]}, "film.json"), /starts with "tap", which is no action/);
});

// A staged film: a page of the kit's parts beside film.json, played by beats that call its runtime.
const STAGE = `<!doctype html><html><head><link rel="stylesheet" href="/__stage/kit/stage.css"><style>
  #terminal { left: 48px; top: 40px; width: 780px; height: 520px; }
  #card { left: 632px; top: 72px; width: 400px; }
</style></head><body><div class="stage">
  <div class="window terminal" id="terminal"><div class="window-bar"><i></i><i></i><i></i></div>
    <div class="window-body"><div class="scroll"><div class="line">$ <span id="cmd"></span></div><div id="term"></div></div></div></div>
  <div class="card arrive" id="card"><div class="card-head"><div class="card-title">Fold</div></div>
    <div class="card-body"><div class="row"><div class="label">Sheep</div><div class="value" id="count">12 out</div></div></div>
    <div class="card-foot"><button class="button primary" id="go" data-then="Herding|All in"><span class="label">Herd them</span></button></div></div>
</div><script src="/__stage/kit/stage.js"></script></body></html>`;

function stagedFolder(beats, page = STAGE) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-stage-"));
  fs.mkdirSync(path.join(dir, "stage"));
  fs.writeFileSync(path.join(dir, "stage", "index.html"), page);
  const file = path.join(dir, "film.json");
  const spec = {title: "Every sheep, back in the fold", story: {before: "Sheep wander.", after: "One press herds them."}, stage: "stage/index.html", screens: {"16:9": 1080}, now: "2026-11-05T10:00:00Z", format: "16:9", ready: ".stage", beats};
  fs.writeFileSync(file, JSON.stringify(spec));
  return {dir, file, spec};
}

const STAGED = [
  {camera: "body", cut: true, pad: 0},
  {caption: "One press herds every sheep."},
  {begin: true},
  {do: ["type", "#cmd", "fold", {cps: 20}]},
  {do: ["print", "#term", ["counting sheep", "12 out"], {gap: 0.2}]},
  {do: ["show", "#card"]},
  {do: ["set", "#count", "12 in"]},
  {click: "#go"},
  {hold: 1.8},
  {mark: "poster"},
  {end: true},
];

test("a staged film plays its page of the kit's parts, offline, through the stage's runtime", {timeout: 180000}, async () => {
  const {dir, file, spec} = stagedFolder(STAGED);
  const film = filmFromSpec(spec, file);
  const {report} = await shoot(film, {format: "16:9", out: path.join(dir, "takes"), rehearse: true, check: true, encode: false});
  assert.deepEqual(report.issues.errors, []);
  assert.deepEqual(report.issues.blocked, [], "a stage reaches nothing outside its folder and the kit");
  assert.equal(report.recording.misses.length, 0);
  assert.deepEqual(report.captions.map(item => item.text), ["One press herds every sheep."]);
});

test("a stage whose hero floats whole, or that draws its own cursor, is told so", {timeout: 180000}, async () => {
  const page = STAGE.replace("height: 520px;", "height: 300px;").replace('<div class="stage">', '<div class="stage"><svg id="pointer" width="18" height="24"><path d="M0 0L0 20L5 15L9 24L12 23L8 14L15 14Z" fill="#fff"/></svg>');
  const {dir, file, spec} = stagedFolder(STAGED, page);
  const {report} = await shoot(filmFromSpec(spec, file), {format: "16:9", out: path.join(dir, "takes"), rehearse: true, encode: false});
  assert.ok(report.issues.warnings.includes("The stage draws a cursor of its own (#pointer); the film draws the hand, so take it out."), report.issues.warnings.join("\n"));
  assert.ok(report.issues.warnings.some(line => /^At poster, the hero #terminal \(780×300\) sits whole on the stage with air on every side/.test(line)), report.issues.warnings.join("\n"));
});

// A diagram on a stage: d3-sankey lays it out from data, inside data-diagram.
const DIAGRAM = `<!doctype html><html><head><link rel="stylesheet" href="/__stage/kit/stage.css"><style>
  .link { fill: none; stroke: #b3aa9b; visibility: hidden; }
  .link.is-ticked { visibility: visible; }
</style></head><body><div class="stage"><div data-diagram><svg width="1080" height="517"></svg></div>
<div class="arrive" style="position: absolute; left: 48px; top: 40px; font-size: 20px;" id="total">0</div></div>
<script src="/__stage/kit/vendor/d3.min.js"></script><script src="/__stage/kit/vendor/d3-sankey.min.js"></script>
<script src="/__stage/kit/stage.js"></script><script>
  const graph = d3.sankey().nodeId(d => d.id).extent([[200, 60], [880, 460]])({
    nodes: [{id: "a"}, {id: "b"}, {id: "c"}],
    links: [{source: "a", target: "b", value: 900}, {source: "a", target: "c", value: 240}],
  });
  d3.select("svg").selectAll("path").data(graph.links).join("path")
    .attr("class", "link").attr("id", (d, i) => "link-" + i)
    .attr("d", d3.sankeyLinkHorizontal()).attr("stroke-width", d => d.width);
</script></body></html>`;

test("a diagram's marks are checked: a drawn flow passes, one that pops in or sits off centre is caught", {timeout: 240000}, async () => {
  const beats = reveal => [
    {camera: "body", cut: true, pad: 0},
    {caption: "Most of them never visit."},
    {begin: true},
    {do: ["show", "#total"]},
    {do: ["count", "#total", 1140]},
    {do: reveal},
    {hold: 1.6},
    {mark: "poster"},
    {end: true},
  ];
  const drawn = stagedFolder(beats(["draw", "#link-0, #link-1"]), DIAGRAM);
  const {report} = await shoot(filmFromSpec(drawn.spec, drawn.file), {format: "16:9", out: path.join(drawn.dir, "takes"), rehearse: true, check: true, encode: false});
  assert.deepEqual(report.issues.errors, []);
  assert.deepEqual(report.issues.warnings.filter(line => /diagram/.test(line)), [], "centred on the caption, inside the gutter");
  assert.ok(!(report.summary ?? []).some(line => /pop-in/.test(line)), (report.summary ?? []).join("\n"));
  const shifted = stagedFolder(beats(["draw", "#link-0, #link-1"]), DIAGRAM.replace("[[200, 60], [880, 460]]", "[[320, 60], [1000, 460]]"));
  const third = await shoot(filmFromSpec(shifted.spec, shifted.file), {format: "16:9", out: path.join(shifted.dir, "takes"), rehearse: true, check: true, encode: false});
  assert.ok(third.report.issues.warnings.some(line => /^At poster, the diagram's marks centre \d+px right of the frame's centre/.test(line)), third.report.issues.warnings.join("\n"));
  const popped = stagedFolder(beats(["tick", "#link-0, #link-1"]), DIAGRAM);
  const second = await shoot(filmFromSpec(popped.spec, popped.file), {format: "16:9", out: path.join(popped.dir, "takes"), rehearse: true, check: true, encode: false});
  assert.ok((second.report.summary ?? []).some(line => /pop-in/.test(line)), "the probe sees inside the diagram");
});

test("a beat that calls what the stage's runtime lacks names what it offers", {timeout: 180000}, async () => {
  const {dir, file, spec} = stagedFolder(STAGED.map(beat => (beat.do?.[0] === "set" ? {do: ["morph", "#count"]} : beat)));
  await assert.rejects(shoot(filmFromSpec(spec, file), {format: "16:9", out: path.join(dir, "takes"), rehearse: true, encode: false}), /The stage has no morph; it offers show, hide, away, swap, print/);
});

test("each worked example a staged film starts from is a film its storyboard draws", () => {
  for (const name of ["onboarding", "people"]) {
    const dir = path.join(path.dirname(new URL(import.meta.url).pathname), "film", "examples", name);
    const spec = JSON.parse(fs.readFileSync(path.join(dir, "film.json"), "utf8"));
    const film = filmFromSpec(spec, path.join(dir, "film.json"));
    assert.equal(film.world().stage.entry, "index.html");
    assert.deepEqual(boardIssues(fs.readFileSync(path.join(dir, "storyboard.md"), "utf8"), spec), [], name);
    assert.ok(fs.statSync(path.join(dir, "sheet.jpg")).size > 50_000, `${name} keeps the contact sheet of its rehearsal`);
  }
});

test("a storyboard the film drifted from says which shot", () => {
  const spec = {beats: [{caption: "Tin ranks them."}, {mark: "ranked"}, {caption: "Tin asks them."}, {mark: "poster"}]};
  const board = "# Ranked\n\nA list.\n\n## 1. Ranked\n- Mark: ranked\n- Caption: Tin ranks them.\n\n## 2. Asked\n- Mark: asked\n- Caption: Tin asks the ones who fit.\n";
  assert.deepEqual(boardIssues(board, spec), [
    'Shot "Asked" is marked asked, and the film never marks asked.',
    'Shot "Asked"\'s caption is not one the film shows: “Tin asks the ones who fit.”.',
    "The film marks poster, which no shot names.",
    "No shot is the poster (- Mark: poster).",
  ]);
  assert.deepEqual(boardIssues("# Nothing yet\n", spec), ['It has no shots: a "## 1. Name" section for each, with "- Mark:" and "- Caption:" lines.']);
  // The directions weighed before the shots are not a shot.
  const weighed = "# Ranked\n\nA list.\n\n## Directions\n- Iceberg.\n- Orbit.\n\n## 1. Ranked\n- Mark: ranked\n- Caption: Tin ranks them.\n\n## 2. Asked\n- Mark: poster\n- Caption: Tin asks them.\n";
  assert.deepEqual(boardIssues(weighed, spec), []);
});

test("a take whose encoder is missing stops with the reason instead of waiting", {timeout: 180000}, async () => {
  const {dir, file, spec} = filmFolder(BEATS);
  const missing = process.env.PATH;
  process.env.PATH = path.join(dir, "no-ffmpeg-here");
  try {
    await assert.rejects(shoot(filmFromSpec(spec, file), {format: "4:5", out: path.join(dir, "takes"), rehearse: true}), /ffmpeg/);
  } finally {
    process.env.PATH = missing;
  }
});

test("captions become WebVTT, and the note plays the film from its own folder", () => {
  assert.equal(vtt([{text: "One", start: 0, end: 45}, {text: "Two", start: 45, end: null}], 90),
    "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nOne\n\n00:00:01.500 --> 00:00:03.000\nTwo\n");
  const report = {seconds: 12.4, captions: [{text: "One press herds every sheep.", start: 0.2, end: 4}], issues: {errors: [], warnings: ["Text under 22px."]}, summary: []};
  const written = note({film: {title: "Every sheep, back in the fold", story: {before: "Sheep wander.", after: "One press herds them."}, site: {url: SITE}, notes: ["The button's shadow is the site's own."]}, report, folder: "films/fold.assets", format: "4:5"});
  assert.match(written, /^# Every sheep, back in the fold\n\n```tin-video\nsrc: fold\.assets\/film\.mp4\nposter: fold\.assets\/poster\.png\ncaptions: fold\.assets\/captions\.vtt\n/);
  assert.match(written, /\*\*Before:\*\* Sheep wander\.\n\n\*\*After:\*\* One press herds them\./);
  assert.match(written, /- 0:00 One press herds every sheep\./);
  assert.match(written, /- Text under 22px\.\n\nThe button's shadow is the site's own\./);
  assert.match(written, /4:5, 1080×1350, 12\.4 s, filmed from a recording of film\.test\./);
});

test("directions are dealt far apart, the same again from the same seed", () => {
  const ideas = deal("idea", {count: 5, seed: "run-1"});
  assert.deepEqual(deal("idea", {count: 5, seed: "run-1"}), ideas);
  assert.notDeepEqual(deal("idea", {count: 5, seed: "run-2"}).map(card => card.name), ideas.map(card => card.name));
  assert.equal(new Set(ideas.map(card => card.name)).size, 5);
  assert.ok(ideas.every(card => FAMILIES.some(family => family.name === card.name)));
  const differ = (a, b) => ["ground", "type", "camera", "accent"].filter(key => a.look[key] !== b.look[key]).length;
  for (const [i, a] of ideas.entries()) for (const b of ideas.slice(i + 1)) assert.ok(differ(a, b) >= 2, `${a.name} and ${b.name} look alike`);
  const product = deal("product", {count: 4, seed: "run-1"});
  assert.ok(product.every(card => STRUCTURES.some(structure => structure.name === card.name)));
  assert.ok(product.every(card => card.look.ground === "the product's own ground" && card.look.type === "the product's own sans"));
  assert.ok(deal("both", {seed: "run-1"}).every(card => card.lands && card.look.ground === "the product's own ground"));
});

test("sketches are filmed for a moment and put side by side, each in its poster frame", {timeout: 180000}, async () => {
  const dir = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-work-")), "sketches");
  fs.mkdirSync(dir);
  for (const [name, words] of [["1-number", "1,140"], ["2-sentence", "Most never visit."]]) {
    fs.mkdirSync(path.join(dir, name));
    fs.writeFileSync(path.join(dir, name, "index.html"), `<!doctype html><html><head><link rel="stylesheet" href="/__stage/kit/stage.css"></head><body><div class="stage"><div class="arrive" style="position: absolute; left: 48px; top: 40px; font-size: 96px;">${words}</div></div><script src="/__stage/kit/stage.js"></script></body></html>`);
    fs.writeFileSync(path.join(dir, name, "caption.txt"), "Your funnel is larger than you think.");
  }
  const said = [];
  const log = console.log;
  console.log = (...args) => { said.push(args.join(" ")); log(...args); };
  try {
    await sketch(dir);
  } finally {
    console.log = log;
  }
  assert.ok(fs.statSync(path.join(dir, "sketches.jpg")).size > 5_000);
  // Without the product's fonts the kit's face falls back, and each sketch says so.
  assert.ok(said.some(line => /1\. 1-number: warning: The stage asks for FK Grotesk Neue .* falls back to/.test(line)), said.join("\n"));
  // Each still and its name, for the founder's pick in Decisions.
  const {sketches} = JSON.parse(fs.readFileSync(path.join(dir, "sketches.json"), "utf8"));
  assert.deepEqual(sketches.map(item => [item.n, item.name, item.still]), [[1, "Number", "1-number/still.jpg"], [2, "Sentence", "2-sentence/still.jpg"]]);
  assert.ok(sketches.every(item => fs.statSync(path.join(dir, item.still)).size > 2_000));
  // A reviewer that didn't sketch them chooses, by the sheet and the maker's notes beside it.
  assert.throws(() => directionsPacket(dir), /No notes to choose by/);
  fs.writeFileSync(path.join(dir, "..", "storyboard.md"), "Kind: motion graphic\n\n## Directions\n1. A number.\n2. A sentence.\n");
  const packet = directionsPacket(dir);
  assert.match(packet, /^# Choose a direction/);
  assert.match(packet, /least like a stock picture/, "the brief from the guide");
  assert.ok(packet.includes(path.join(dir, "sketches.jpg")) && packet.includes(path.join(dir, "2-sentence", "still.jpg")));
  assert.ok(packet.includes(path.join(dir, "review.json")), "where the reviewer answers");
  // The reviewer's recommendation and each sketch go into the note the founder picks from.
  fs.writeFileSync(path.join(dir, "review.json"), JSON.stringify({choice: 9, runner_up: 1, why: "A sentence says it.", keep: ""}));
  assert.throws(() => deliverDirections(dir, path.join(os.tmpdir(), "x.md")), /one of the sketches' numbers/);
  fs.writeFileSync(path.join(dir, "review.json"), JSON.stringify({choice: 2, runner_up: 1, why: "A sentence says it.", keep: ""}));
  fs.writeFileSync(path.join(dir, "2-sentence", "about.txt"), "One sentence carries the claim.\n");
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-project-"));
  const notePath = path.join(project, "films", "funnel.md");
  fs.mkdirSync(path.join(project, "films", "funnel.assets"), {recursive: true});
  // An earlier version's files leave the folder: each version holds exactly its own.
  fs.writeFileSync(path.join(project, "films", "funnel.assets", "1-earlier.jpg"), "old");
  deliverDirections(dir, notePath);
  assert.ok(!fs.existsSync(path.join(project, "films", "funnel.assets", "1-earlier.jpg")));
  const delivered = fs.readFileSync(notePath, "utf8");
  assert.match(delivered, /^```tin-directions\nrecommended: 2\nrunner_up: 1\nwhy: A sentence says it\.\n```$/m);
  assert.match(delivered, /^## 2\. Sentence\n\n!\[Sentence\]\(\.\/funnel\.assets\/2-sentence\.jpg\)\n\nOne sentence carries the claim\.$/m);
  for (const name of ["1-number.jpg", "1-number.html", "2-sentence.jpg", "2-sentence.html", "notes.json"]) assert.ok(fs.existsSync(path.join(project, "films", "funnel.assets", name)), name);
  assert.match(JSON.parse(fs.readFileSync(path.join(project, "films", "funnel.assets", "notes.json"), "utf8")).storyboard, /## Directions/);
  assert.notEqual(process.exitCode, 1, "the note passes Tin's check");
});

test("a delivered film comes back as a staged film to change", () => {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-project-"));
  const folder = path.join(project, "films", "funnel.assets");
  fs.mkdirSync(folder, {recursive: true});
  fs.writeFileSync(path.join(folder, "film.json"), JSON.stringify({title: "Funnel", stage: "elsewhere/index.html", beats: []}));
  fs.writeFileSync(path.join(folder, "stage.html"), "<!doctype html><div class=stage></div>");
  fs.writeFileSync(path.join(folder, "storyboard.json"), JSON.stringify({storyboard: "# Funnel\n"}));
  const work = path.join(project, "work");
  restore(path.join(project, "films", "funnel.md"), work);
  assert.equal(JSON.parse(fs.readFileSync(path.join(work, "film.json"), "utf8")).stage, "stage/index.html");
  assert.equal(fs.readFileSync(path.join(work, "stage", "index.html"), "utf8"), "<!doctype html><div class=stage></div>");
  assert.equal(fs.readFileSync(path.join(work, "storyboard.md"), "utf8"), "# Funnel\n");
  fs.rmSync(path.join(folder, "stage.html"));
  assert.throws(() => restore(path.join(project, "films", "funnel.md"), work), /no stage\.html/);
});

test("the kit runs through the link npx puts in node_modules/.bin", () => {
  const bin = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-bin-")), "tin-film");
  fs.symlinkSync(path.resolve("web/film/tin-film.js"), bin);
  const run = spawnSync(process.execPath, [bin, "help"], {encoding: "utf8"});
  assert.equal(run.status, 0, run.stderr);
  assert.match(run.stdout, /upload/);
});

test("a second look is a brief for a reviewer that didn't make the film, with the stills to open", () => {
  const {dir, file} = filmFolder(BEATS);
  const take = path.join(dir, "takes", "rehearse", "film", "4x5");
  fs.mkdirSync(path.join(take, "marks"), {recursive: true});
  const png = Buffer.from("89504e470d0a1a0a0000000049484452", "hex");
  fs.writeFileSync(path.join(take, "sheet.png"), png);
  for (const name of ["01-poster.png", "02-how.jpg", "notes.txt"]) fs.writeFileSync(path.join(take, "marks", name), png);
  fs.writeFileSync(path.join(take, "report.json"), JSON.stringify({issues: {errors: [], warnings: ["Text under 22px."]}, summary: ["0.5s jump"]}));
  const packet = reviewPacket(file, take);
  assert.match(packet, /^# Review this film/);
  assert.match(packet, /No eyebrows/, "the brief from the guide");
  for (const name of ["sheet.png", path.join("marks", "01-poster.png"), path.join("marks", "02-how.jpg")]) assert.ok(packet.includes(path.join(take, name)), name);
  assert.ok(!packet.includes("notes.txt"), "only images");
  assert.match(packet, /- Text under 22px\.\n- 0\.5s jump/);
  assert.ok(packet.includes(path.resolve(file)));
  assert.ok(!packet.includes("example.jpg"));
  // A film started from a worked example names its storyboard and the example's sheet, the bar.
  fs.writeFileSync(path.join(dir, "storyboard.md"), "# Fold\n");
  fs.writeFileSync(path.join(dir, "example.jpg"), png);
  const held = reviewPacket(file, take);
  assert.ok(held.includes(path.join(dir, "storyboard.md")) && held.includes(path.join(dir, "example.jpg")));
});

test("upload sends the note and its folder to the link, and says what Tin answered", async () => {
  const project = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-project-"));
  fs.mkdirSync(path.join(project, "films", "funnel.assets"), {recursive: true});
  fs.writeFileSync(path.join(project, "films", "funnel.md"), "# Funnel\n");
  fs.writeFileSync(path.join(project, "films", "funnel.assets", "1-number.jpg"), "jpeg");
  const received = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", chunk => chunks.push(chunk));
    request.on("end", () => {
      received.push({method: request.method, url: request.url, type: request.headers["content-type"], body: Buffer.concat(chunks)});
      const refused = request.url.endsWith("refuse");
      response.writeHead(refused ? 422 : 200, {"content-type": "application/json"});
      response.end(JSON.stringify(refused ? {detail: "The note can't be kept.", problems: ["funnel.assets/2-x.jpg is missing"]} : {ok: true, next: "Kept as version 1.", decisions: "https://app.tin.test/decisions", dropped: []}));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}/api/film-uploads`;
  try {
    await assert.rejects(upload(path.join(project, "films", "funnel.md"), "https://elsewhere.test/x"), /usage/);
    const kept = await upload(path.join(project, "films", "funnel.md"), `${base}/token-abc`);
    assert.equal(kept.next, "Kept as version 1.");
    assert.equal(received[0].method, "PUT");
    assert.equal(received[0].type, "application/gzip");
    const listing = spawnSync("tar", ["-tzf", "-"], {input: received[0].body}).stdout.toString().split("\n").filter(Boolean).sort();
    assert.deepEqual(listing, ["funnel.assets/", "funnel.assets/1-number.jpg", "funnel.md"]);
    const refused = await upload(path.join(project, "films", "funnel.md"), `${base}/token-refuse`);
    assert.deepEqual(refused.problems, ["funnel.assets/2-x.jpg is missing"]);
    assert.equal(process.exitCode, 1, "a refused upload exits 1");
  } finally {
    process.exitCode = 0;
    server.close();
  }
});

test("inspect keeps the face a page's text is set in, with its files, not the body's", {timeout: 120000}, async () => {
  // Shaped like a Next.js site: the body names one face, a wrapper sets the visible text in a
  // next/font face with a hashed name, and a fallback face points at a system font.
  const font = fs.readFileSync(path.resolve("src/tin_lite/static/fonts/geist-sans-regular.woff2"));
  const pages = {
    "/": ["text/html", `<!doctype html><html><head><title>Fold</title><link rel="stylesheet" href="/site.css"><style>
      @font-face { font-family: "__brandSans_4f5e8e"; src: url(/media/a1b2c3-s.p.woff2) format("woff2"); font-weight: 400; font-style: normal; }
      @font-face { font-family: "__brandSans_Fallback_4f5e8e"; src: local("Arial"); }
      .wrap { font-family: "__brandSans_4f5e8e", "__brandSans_Fallback_4f5e8e", sans-serif; }
      @media (prefers-color-scheme: dark) { body { background: #141210; color: #f6f1e7; } }
    </style></head><body><div class="wrap"><h1>Herd them with a circle</h1><p>Draw round the sheep and they follow.</p><pre style="font-family: ui-monospace, monospace">$ npx sheepdog --herd the-whole-flock --circle wide --follow true</pre></div></body></html>`],
    "/site.css": ["text/css", `@font-face { font-family: "Body Face"; src: url(/fonts/body.woff2) format("woff2"); } body { font-family: "Body Face", sans-serif; margin: 0; }`],
    "/media/a1b2c3-s.p.woff2": ["font/woff2", font],
  };
  const server = http.createServer((request, response) => {
    const page = pages[new URL(request.url, "http://x").pathname];
    if (!page) return response.writeHead(404).end();
    response.writeHead(200, {"content-type": page[0]}).end(page[1]);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-look-"));
  try {
    const report = await inspect(`http://127.0.0.1:${server.address().port}/`, {dir});
    assert.equal(report.look.font, "Brand Sans");
    // More code than prose on the page still leaves the reading face as --sans.
    assert.deepEqual(report.look.fonts.map(item => [item.family, item.mono, item.kept]).sort(), [["Brand Sans", false, ["400"]], ["ui-monospace", true, []]]);
    assert.match(report.look.tokens, /--sans: "Brand Sans", system-ui, sans-serif;/);
    // Its dark theme too, in the same type.
    assert.equal(report.look.dark.background, "rgb(20, 18, 16)");
    assert.match(report.look.dark.tokens, /--ground: rgb\(20, 18, 16\);.*--sans: "Brand Sans", system-ui, sans-serif;/);
    const css = fs.readFileSync(path.join(dir, "fonts.css"), "utf8");
    assert.match(css, /font-family: "Brand Sans"; src: url\("fonts\/brand-sans-400-[0-9a-f]{8}\.woff2"\) format\("woff2"\); font-weight: 400;/);
    const [file] = fs.readdirSync(path.join(dir, "fonts"));
    assert.deepEqual(fs.readFileSync(path.join(dir, "fonts", file)), font);
    // The body's own face set no visible text, and the fallback face is a system font: neither is kept.
    assert.doesNotMatch(css, /Body Face|Fallback/);
  } finally {
    server.close();
  }
});

test("a face's name reads as people say it, and its rules resolve against their sheet", () => {
  assert.equal(faceName("__fkGroteskNeue_4f5e8e"), "Fk Grotesk Neue");
  assert.equal(faceName("fkGroteskNeue"), "Fk Grotesk Neue");
  assert.equal(faceName("Geist Mono"), "Geist Mono");
  assert.equal(faceName("Inter"), "Inter");
  const [face] = fontFaces(`@font-face{font-family:'Inter';src:url(data:x) format("woff2"),url("../f/inter.woff2?v=1") format("woff2");font-weight:100 900;unicode-range:U+0000-00FF}`, "https://site.test/css/a.css");
  assert.deepEqual(face, {family: "Inter", weight: "100 900", style: "normal", range: "U+0000-00FF", sources: [{url: "https://site.test/f/inter.woff2?v=1", format: "woff2"}]});
});

test("an eyebrow fails the take; a table's column header does not", {timeout: 180000}, async () => {
  const dir = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-eyebrow-")), "sketches");
  const page = body => `<!doctype html><html><head><link rel="stylesheet" href="/__stage/kit/stage.css"></head><body><div class="stage"><div class="arrive" style="position: absolute; left: 48px; top: 40px;">${body}</div></div><script src="/__stage/kit/stage.js"></script></body></html>`;
  const sketches = {
    "1-kicker": `<div style="font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;">Mentions</div><div style="font-size: 44px;">“Sample Tool”</div>`,
    "2-table": `<table><thead><tr><th style="font-size: 14px; letter-spacing: 0.12em; text-transform: uppercase; text-align: left;">Name</th></tr></thead><tbody><tr><td style="font-size: 30px;">Mira Tallow</td></tr></tbody></table>`,
  };
  for (const [name, body] of Object.entries(sketches)) {
    fs.mkdirSync(path.join(dir, name), {recursive: true});
    fs.writeFileSync(path.join(dir, name, "index.html"), page(body));
    fs.writeFileSync(path.join(dir, name, "caption.txt"), "Your funnel is larger than you think.");
  }
  const said = [];
  const log = console.log;
  console.log = (...args) => { said.push(args.join(" ")); log(...args); };
  try {
    await sketch(dir);
  } finally {
    console.log = log;
  }
  assert.ok(said.some(line => line.includes("1. 1-kicker: error: At poster, an eyebrow: “Mentions” sits small above ““Sample Tool””")), said.join("\n"));
  assert.ok(!said.some(line => /2-table: error/.test(line)), said.join("\n"));
  assert.equal(process.exitCode, 1, "a sketch with an error fails the command");
  process.exitCode = 0;
});

test("a sketch with words on words or text too small to read fails; a clean one passes", {timeout: 180000}, async () => {
  const dir = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-layout-")), "sketches");
  const page = body => `<!doctype html><html><head><link rel="stylesheet" href="/__stage/kit/stage.css"><style>.stage { background: #141210; color: #f6f1e7; } .card { position: absolute; left: 140px; top: 120px; width: 560px; height: 260px; background: #1b1917; border-radius: 16px; }</style></head><body><div class="stage">${body}</div><script src="/__stage/kit/stage.js"></script></body></html>`;
  const sketches = {
    "1-crowded": page('<div class="card"></div><p style="position: absolute; left: 180px; top: 170px; margin: 0; font-size: 40px;">Which tool should I use?</p><p style="position: absolute; left: 196px; top: 176px; margin: 0; font-size: 40px;">Use Sample Tool today.</p><p style="position: absolute; left: 180px; top: 300px; margin: 0; font-size: 9px;">A sample answer, made up for this film</p><p style="position: absolute; left: 180px; top: 500px; margin: 0; font-size: 28px;">Files / reports / AI-VISIBILITY.md</p>'),
    "2-clean": page('<div class="card"><p style="margin: 64px 56px; font-size: 40px;">Use Sample Tool.</p></div>'),
  };
  for (const [name, html] of Object.entries(sketches)) {
    fs.mkdirSync(path.join(dir, name), {recursive: true});
    fs.writeFileSync(path.join(dir, name, "index.html"), html);
    fs.writeFileSync(path.join(dir, name, "caption.txt"), "AI answers are the new front door.");
  }
  const said = [];
  const log = console.log;
  console.log = (...args) => { said.push(args.join(" ")); log(...args); };
  try {
    await sketch(dir);
  } finally {
    console.log = log;
  }
  assert.ok(said.some(line => /1-crowded: error: .*layout\/text-overlap/.test(line)), said.join("\n"));
  assert.ok(said.some(line => /1-crowded: error: \d+ text runs are under \d+px/.test(line)), said.join("\n"));
  assert.ok(said.some(line => /1-crowded: error: At poster, the crop cuts “Files \/ reports \/ AI-VISIBILITY\.md”/.test(line)), said.join("\n"));
  assert.ok(!said.some(line => /2-clean: error/.test(line)), said.join("\n"));
  assert.equal(process.exitCode, 1);
  process.exitCode = 0;
});
