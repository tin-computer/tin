// Feature films (web/film): the packaged dashboard on a demo world, filmed frame by frame under
// the virtual clock. No live credentials, projects, fonts or encoder.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {shoot, stage, FORMATS} from "./film/film.js";
import * as people from "./film/films/people-requests.js";

const short = play => ({name: "short", now: people.now, timezone: people.timezone, world: people.world, play});

async function take(film, format = "4:5") {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "tin-film-"));
  try {
    return (await shoot(film, {format, out, encode: false})).report;
  } finally {
    fs.rmSync(out, {recursive: true, force: true});
  }
}

test("a take is the same frame for frame, with no outside requests", {timeout: 180000}, async () => {
  const film = short(async f => {
    f.inside = "#main";
    await f.camera([".people-view h1", ".people-table thead", ".people-table tbody tr:nth-child(-n+4)"], {cut: true});
    f.caption("Everyone who engaged, ranked by your ICP.");
    await f.hold(0.3);
    await f.click('[data-people-pick="p-chloe"]');
    await f.ready(".people-picked");
    await f.mark("poster");
  });
  const first = await take(film);
  const second = await take(film);
  assert.deepEqual(first.issues.errors, []);
  assert.deepEqual(first.issues.blocked, []);
  assert.ok(first.frames > 20);
  assert.deepEqual(first.hashes, second.hashes);
  // The ticked person changed what was on screen.
  assert.notEqual(first.hashes[0], first.hashes.at(-1));
});

test("the cursor can't reach outside the frame, and a film needs its poster", {timeout: 180000}, async () => {
  const report = await take(short(async f => {
    await f.camera('.people-table tr[data-person="p-daniel"] td.is-name', {cut: true, text: true, pad: 8});
    await f.move('.people-table tr[data-person="p-ben"] td.is-linkedin', {to: [0.9, 0.5]});
  }));
  assert.ok(report.issues.errors.some(line => /The cursor reaches .*p-ben.* outside the 4:5 frame/.test(line)), report.issues.errors);
  assert.ok(report.issues.errors.includes("The film never marks its poster."));
});

test("a click that moves the page under the cursor is reported", {timeout: 180000}, async () => {
  const report = await take(short(async f => {
    await f.camera([".people-view h1", ".people-table thead", ".people-table tbody tr:nth-child(-n+4)"], {cut: true});
    // Whatever the dashboard does, this click pushes the grid down 60px.
    await f.app.evaluate(() => document.querySelector(".people-view h1").addEventListener("click", () => {
      document.querySelector(".people-view").style.paddingTop = "60px";
    }, {once: true}));
    await f.click(".people-view h1");
    await f.hold(0.4);
    await f.mark("poster");
  }));
  assert.ok(report.issues.warnings.some(line => /h1.* moved it \d+px: the page shifted under the cursor/.test(line)), report.issues.warnings);
});

test("the page sees the pointer only where the hand stops", {timeout: 180000}, async () => {
  const row = '.people-table tr[data-person="p-daniel"]';
  let crossed = false;
  let arrived = false;
  await take(short(async f => {
    await f.camera([".people-view h1", ".people-table thead", ".people-table tbody tr:nth-child(-n+4)"], {cut: true});
    await f.move(`${row} td.is-name`);
    // Every frame, whether a score's reason card is open.
    f.tracks.add(async () => {
      crossed ||= await f.app.locator(".people-hover").isVisible();
      return false;
    });
    // Across Daniel's score to the far end of his row: nothing opens on the way.
    await f.move(`${row} td.is-linkedin`);
    await f.hold(0.3);
    const passing = crossed;
    // Onto the score: its card opens.
    await f.move(`${row} td.is-fit`);
    await f.hold(0.3);
    arrived = await f.app.locator(".people-hover").isVisible();
    crossed = passing;
    await f.mark("poster");
  }));
  assert.equal(crossed, false);
  assert.equal(arrived, true);
});

// After Stageshow's motion rules: one push-in per story, within 2x and no faster than 1.1s, and the
// cursor holds still while the view moves.
test("a camera that breaks Stageshow's rules is reported", {timeout: 180000}, async () => {
  const report = await take(short(async f => {
    const main = await f.rect("#main");
    await f.camera({x: main.x, y: 0, width: main.width, height: 1}, {cut: true, pad: 0});
    await f.hold(0.2);
    await f.camera('.people-table tr[data-person="p-daniel"] td.is-fit', {duration: 0.5, pad: 8, wait: false});
    await f.move('.people-table tr[data-person="p-daniel"] td.is-name');
    await f.camera({x: main.x, y: 0, width: main.width, height: 1}, {pad: 0});
    await f.camera('.people-table tr[data-person="p-lea"] td.is-name', {pad: 60});
    await f.mark("poster");
  }));
  const said = report.issues.warnings.join("\n");
  assert.match(said, /zooms \d\.\dx in 0\.5s/);
  assert.match(said, /pushes in 2 times; one push-in per story/);
  assert.match(said, /the cursor moves toward .*p-daniel.* while the camera moves/i);
});

test("a margin sets the screen as a window on the backdrop, with room for the band", () => {
  const {screen, margin, room, viewport} = stage(FORMATS["16:9"], {width: 1440, height: 534, margin: 40});
  assert.deepEqual(screen, {width: 1440, height: 534});
  assert.deepEqual(margin, {top: 40, right: 40, bottom: 40, left: 40});
  assert.deepEqual(room, {width: 1520, height: 614});
  // Or per side: a full-bleed screen with a gutter only under it.
  assert.deepEqual(stage(FORMATS["4:5"], {width: 600, height: 713, margin: {bottom: 25}}).room, {width: 600, height: 738});
  assert.equal(viewport.width, 1520);
  // A frame as wide as the room still has the band's height under it.
  assert.ok(viewport.height >= room.height + FORMATS["16:9"].band * (room.width / FORMATS["16:9"].width));
});

test("the caption band sits below the dashboard's screen", () => {
  for (const size of Object.values(FORMATS)) {
    const {screen, viewport} = stage(size);
    assert.equal(screen.width, viewport.width);
    // At the widest shot the screen fills the picture and the band is extra window.
    assert.ok(Math.abs(screen.height / screen.width - (size.height - size.band) / size.width) < 0.002);
    assert.ok(viewport.height >= screen.height + size.band * (screen.width / size.width) - 1);
  }
});
