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
import {boardIssues, note} from "./film/tin-film.js";

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
