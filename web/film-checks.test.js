// The film's checks on synthetic measurements: the product's motion on its page, the framing and
// key frames on the picture, and what a film means to have accepted with its reason.
import assert from "node:assert/strict";
import test from "node:test";
import {checkFrames, summary} from "./film/checks.js";

const PAGE = [1440, 618];
const PICTURE = [1920, 1080];
const box = (k, b, extra = {}) => ({k, t: "b", b, v: b, o: 1, bg: "rgb(30, 30, 30)", ...extra});
const text = (k, x, b, extra = {}) => ({k, t: "t", b, v: b, o: 1, x, fs: 16, c: "rgb(240, 240, 240)", fw: 400, ff: "Geist", ...extra});

// `pageAt(n)` and `pictureAt(n)` give each frame's rows.
function take(frames, {pageAt = () => [], pictureAt = () => []} = {}) {
  return Array.from({length: frames}, (_, n) => ({
    picture: {f: n, c: PICTURE, e: pictureAt(n)},
    pages: {screen: {f: n, c: PAGE, e: pageAt(n)}},
  }));
}

test("a panel that pops in on the product's page is reported, a row's background changing state isn't", () => {
  const page = n => [
    box("r/body#0/main#0", [0, 0, 1000, 618]),
    ...(n >= 10 ? [box("r/body#0/main#0/aside#0", [560, 40, 420, 500]), box("r/body#0/main#0/tr#0", [20, 100, 176, 44], {bg: "rgb(60, 60, 60)"})] : []),
  ];
  const findings = checkFrames(take(30, {pageAt: page}), {name: "t", fps: 30});
  const pops = findings.filter(item => item.rule === "animate/pop-in");
  assert.equal(pops.length, 1, JSON.stringify(findings.map(item => [item.rule, item.element])));
  assert.match(pops[0].element, /^surface 420x500/);
  assert.equal(pops[0].where, "screen");
  assert.deepEqual(pops[0].frames, [10, 10]);
});

test("the camera moving the picture is no motion of the product's", () => {
  // Every row of the picture slides as a push-in would, while the page holds still.
  const picture = n => [text("r/room#0/frame#0/td#0", "Daniel Okafor", [100 + n * 6, 300, 160, 24])];
  const page = () => [box("r/body#0/main#0", [0, 0, 1000, 618]), text("r/body#0/main#0/td#0", "Daniel Okafor", [100, 300, 160, 24])];
  const findings = checkFrames(take(40, {pageAt: page, pictureAt: picture}), {name: "t", fps: 30});
  assert.deepEqual(findings.filter(item => item.where === "screen"), []);
});

test("a key frame taken mid-change is reported, unless what changes is the film's own highlight", () => {
  const moving = n => [text("r/room#0/toast#0", "Queued", [800, 600 - Math.min(n, 20) * 2, 120, 24])];
  const marks = {queued: 15};
  const caught = checkFrames(take(30, {pictureAt: moving}), {name: "t", fps: 30, marks});
  assert.ok(caught.some(item => item.rule === "film/mark-settled" && /"queued"/.test(item.measured)), JSON.stringify(caught));
  const settled = checkFrames(take(30, {pictureAt: n => moving(Math.max(n, 20))}), {name: "t", fps: 30, marks});
  assert.ok(!settled.some(item => item.rule === "film/mark-settled"));
  const highlight = n => [box("r/room#0/mark#0", [790, 590, 140, 40], {o: 1 - n / 40, film: 1})];
  assert.ok(!checkFrames(take(30, {pictureAt: highlight}), {name: "t", fps: 30, marks}).some(item => item.rule === "film/mark-settled"));
});

test("a cursor resting on words at a key frame is reported", () => {
  const cursor = tip => ({k: "r/cursor", t: "g", b: [tip[0], tip[1], 20, 28], v: [tip[0], tip[1], 20, 28], o: 1, id: "cursor:hand", tp: tip, pf: "rgb(0, 0, 0)", ps: "rgb(255, 255, 255)"});
  const words = text("r/room#0/a#0", "What changed", [1270, 470, 110, 20]);
  const on = checkFrames(take(20, {pictureAt: () => [words, cursor([1300, 480])]}), {name: "t", fps: 30, marks: {poster: 10}});
  assert.ok(on.some(item => item.rule === "film/mark-cursor-on-text" && /What changed/.test(item.measured)), JSON.stringify(on.map(item => item.rule)));
  const off = checkFrames(take(20, {pictureAt: () => [words, cursor([1392, 482])]}), {name: "t", fps: 30, marks: {poster: 10}});
  assert.ok(!off.some(item => item.rule === "film/mark-cursor-on-text"));
});

test("words cut by the frame are reported, and accepted with the film's reason where it means them", () => {
  // Half of a line runs past the picture's right edge for a second.
  const picture = () => [text("r/room#0/td#0", "oct 25 · viewed you", [1840, 500, 160, 24])];
  const open = checkFrames(take(40, {pictureAt: picture}), {name: "t", fps: 30});
  assert.ok(open.some(item => item.rule === "layout/text-cut" && !item.accepted));
  const reason = "the following camera crops through the screen as it goes";
  const accepted = checkFrames(take(40, {pictureAt: picture}), {name: "t", fps: 30, accepts: [{rule: "layout/text-cut", frames: [0, 39], reason}]});
  const cut = accepted.filter(item => item.rule === "layout/text-cut");
  assert.ok(cut.length && cut.every(item => item.accepted === reason));
  assert.deepEqual(summary(accepted).filter(line => /accepted/.test(line)), [`${cut.length} accepted: ${reason}`]);
});

test("a stretch starting later in the take reports its findings in the take's own time", () => {
  const page = n => [box("r/body#0/main#0", [0, 0, 1000, 618]), ...(n >= 10 ? [box("r/body#0/main#0/aside#0", [560, 40, 420, 500])] : [])];
  const [pop] = checkFrames(take(30, {pageAt: page}), {name: "t", fps: 30, offset: 300}).filter(item => item.rule === "animate/pop-in");
  assert.deepEqual(pop.frames, [310, 310]);
  assert.equal(pop.from, 10.33);
});
