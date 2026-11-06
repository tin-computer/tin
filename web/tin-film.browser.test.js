// tin-film: a film written as JSON plays a live site from its recording, says what is wrong with a
// spec before it reaches the screen, and becomes a note that plays the film from its own folder.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {filmFromSpec, shoot, vtt} from "./film/film.js";
import {recordingKey} from "./film/recording.js";
import {boardIssues, deliverDirections, directionsRequest, note, restore, reviewRequest, sketch} from "./film/tin-film.js";
import {FAMILIES, STRUCTURES, deal} from "./film/directions.js";

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
  await sketch(dir);
  assert.ok(fs.statSync(path.join(dir, "sketches.jpg")).size > 5_000);
  // Each still and its name, for the founder's pick in Decisions.
  const {sketches} = JSON.parse(fs.readFileSync(path.join(dir, "sketches.json"), "utf8"));
  assert.deepEqual(sketches.map(item => [item.n, item.name, item.still]), [[1, "Number", "1-number/still.jpg"], [2, "Sentence", "2-sentence/still.jpg"]]);
  assert.ok(sketches.every(item => fs.statSync(path.join(dir, item.still)).size > 2_000));
  // The second look chooses among them by the maker's notes beside the folder.
  assert.throws(() => directionsRequest(dir), /No notes to choose by/);
  fs.writeFileSync(path.join(dir, "..", "storyboard.md"), "Kind: motion graphic\n\n## Directions\n1. A number.\n2. A sentence.\n");
  const request = directionsRequest(dir);
  assert.equal(request.kind, "directions");
  assert.equal(request.sketches, 2);
  assert.equal(request.images.length, 1);
  assert.match(request.spec, /## Directions/);
  // The second look's recommendation and each sketch go into the note the founder picks from.
  fs.writeFileSync(path.join(dir, "review.json"), JSON.stringify({verdict: "chosen", choice: 2, runner_up: 1, summary: "A sentence says it.", keep: "", findings: []}));
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

test("a second look sends the take's sheet and marked stills with the spec and what the checks found", () => {
  const {dir, file} = filmFolder(BEATS);
  const take = path.join(dir, "takes", "rehearse", "film", "4x5");
  fs.mkdirSync(path.join(take, "marks"), {recursive: true});
  const png = Buffer.from("89504e470d0a1a0a0000000049484452", "hex");
  fs.writeFileSync(path.join(take, "sheet.png"), png);
  for (const name of ["01-poster.png", "02-how.jpg", "notes.txt"]) fs.writeFileSync(path.join(take, "marks", name), png);
  fs.writeFileSync(path.join(take, "report.json"), JSON.stringify({issues: {errors: [], warnings: ["Text under 22px."]}, summary: ["0.5s jump"]}));
  const request = reviewRequest(file, take);
  assert.equal(request.images.length, 3, "the sheet and the two marked stills");
  assert.deepEqual(request.images.map(image => image.media_type), ["image/png", "image/png", "image/jpeg"]);
  assert.ok(request.images.every(image => image.data_base64 === png.toString("base64")));
  assert.equal(request.checks, "Text under 22px.\n0.5s jump");
  assert.equal(request.spec, fs.readFileSync(file, "utf8"));
  assert.match(request.request_id, /^[0-9a-f]{32}$/);
  assert.equal(reviewRequest(file, take).request_id, request.request_id, "an unchanged take asks the same question");
  assert.equal(request.storyboard, "");
  assert.equal(request.example, null);
  // A film started from a worked example sends its storyboard and the example's sheet, the bar.
  fs.writeFileSync(path.join(dir, "storyboard.md"), "# Fold\n\n## 1. Herd\n- Mark: poster\n- Caption: One press herds every sheep.\n");
  fs.writeFileSync(path.join(dir, "example.jpg"), png);
  const held = reviewRequest(file, take);
  assert.match(held.storyboard, /## 1\. Herd/);
  assert.deepEqual(held.example, {media_type: "image/jpeg", data_base64: png.toString("base64")});
  assert.notEqual(held.request_id, request.request_id, "a new storyboard asks a new question");
});
