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
import {note} from "./film/tin-film.js";

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
    /film.json needs a title, site.url, the https address the film opens on, now, the moment the page's clock shows, as an ISO time, beats\./);
  assert.throws(() => filmFromSpec({...filmFolder(BEATS).spec, beats: [{tap: "#go"}]}, "film.json"), /starts with "tap", which is no action/);
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
