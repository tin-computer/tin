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
    await f.click('[data-people-pick="p-daniel"]');
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
    await f.camera(".people-table tbody tr:nth-child(-n+2)", {cut: true});
    await f.move('.people-table tr[data-person="p-ben"] td.is-name');
  }));
  assert.ok(report.issues.errors.some(line => /The cursor reaches .*p-ben.* outside the 4:5 frame/.test(line)), report.issues.errors);
  assert.ok(report.issues.errors.includes("The film never marks its poster."));
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
