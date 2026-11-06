// Films as data: beats play as the director's calls, and a broken film is refused before filming.
import assert from "node:assert/strict";
import test from "node:test";
import {check, perform, player} from "./film/spec.js";

// A director that writes down what it was asked to do.
function recorder({wide = true, brisk = false, shown = []} = {}) {
  const calls = [];
  const note = name => (...args) => { calls.push([name, ...args]); };
  const f = new Proxy({wide, brisk, calls}, {
    get(target, key) {
      if (key in target) return target[key];
      if (key === "shows") return async selector => shown.includes(selector);
      if (key === "morph") return async (change, options) => { calls.push(["morph:start", options]); await change(); calls.push(["morph:end"]); };
      if (key === "term") return {print: note("term.print"), work: note("term.work"), enter: note("term.enter"), box: note("term.box")};
      return note(key);
    },
  });
  return f;
}

test("a beat is the director's call: its first key the action, its value the argument, the rest options", async () => {
  const f = recorder();
  await perform(f, [
    {open: "/people", ready: ".people-table"},
    {highlight: ".row", hold: 1.2},
    {click: "#send", to: [0.9, 0.3]},
    {hold: 2},
    {begin: true},
    {look: null},
    {wait: [1.6, 1.0]},
    {show: ["app", "app.tin.computer/system"]},
    {"term.work": [14, 1.2], verb: "Reading"},
    {"term.box": true},
    {"term.print": [[["a", "dim"]], []], over: 0.3},
    {follow: {zoom: 1.6}},
  ]);
  assert.deepEqual(f.calls, [
    ["open", "/people", {ready: ".people-table"}],
    ["highlight", ".row", {hold: 1.2}],
    ["click", "#send", {to: [0.9, 0.3]}],
    ["hold", 2],
    ["begin"],
    ["look", null],
    ["wait", 1.6, 1.0],
    ["show", "app", "app.tin.computer/system"],
    ["term.work", 14, 1.2, {verb: "Reading"}],
    ["term.box"],
    ["term.print", [[["a", "dim"]], []], {over: 0.3}],
    ["follow", {zoom: 1.6}],
  ]);
});

test("a beat plays only in its takes, only while its selector shows, and groups and morphs nest", async () => {
  const beats = [
    {hold: 1, when: "wide"},
    {hold: 2, when: "tall"},
    {when: "full", beats: [{mark: "full"}, {hold: 3}]},
    {mark: "brisk", when: "brisk"},
    {highlight: ".accepted", if: ".accepted"},
    {highlight: ".missing", if: ".missing"},
    {morph: [{run: "change"}, {hold: 0.3}], caption: "They accept."},
  ];
  const changed = [];
  const values = {change: f => changed.push(f.wide)};
  const wide = recorder({shown: [".accepted"]});
  await perform(wide, beats, values);
  assert.deepEqual(wide.calls, [
    ["hold", 1],
    ["mark", "full"],
    ["hold", 3],
    ["highlight", ".accepted"],
    ["morph:start", {caption: "They accept."}],
    ["hold", 0.3],
    ["morph:end"],
  ]);
  const tall = recorder({wide: false, brisk: true});
  await perform(tall, beats, values);
  assert.deepEqual(tall.calls.map(call => call.slice(0, 2)), [["hold", 2], ["mark", "brisk"], ["morph:start", {caption: "They accept."}], ["hold", 0.3], ["morph:end"]]);
  assert.deepEqual(changed, [true, false]);
});

test("named values stand anywhere in a beat, and a function value measures when the beat plays", async () => {
  const f = recorder();
  let measured = 0;
  const values = {
    BLANK: {x: 1826, y: 826, width: 12, height: 12},
    column: async () => ({x: 10, y: ++measured, width: 100, height: 1}),
    lines: [["first"], ["second"]],
  };
  await perform(f, [
    {move: "@BLANK"},
    {camera: "@column", cut: true},
    {camera: "@column", cut: true},
    {look: ["#name", "@BLANK"]},
    {"term.print": "@lines"},
  ], values);
  assert.deepEqual(f.calls, [
    ["move", values.BLANK],
    ["camera", {x: 10, y: 1, width: 100, height: 1}, {cut: true}],
    ["camera", {x: 10, y: 2, width: 100, height: 1}, {cut: true}],
    ["look", ["#name", values.BLANK]],
    ["term.print", values.lines],
  ]);
});

test("a broken film is refused before anything is filmed, naming each beat that's wrong", () => {
  const values = {tagRows: () => {}};
  assert.deepEqual(check([
    {hold: 1},
    {hodl: 1},
    {when: "sideways", hold: 1},
    {click: "@nowhere"},
    {run: "missing"},
    {run: "tagRows"},
    {when: "full", beats: [{"term.dance": true}]},
    {morph: [{camera: "@nowhere"}]},
    "hold",
  ], values), [
    'beats[1] starts with "hodl", which is no action (address, begin, camera, click, do, endCard, end, focus, follow, gone, hide, highlight, hold, look, mark, move, open, raise, read, ready, say, scroll, scrollPage, show, still, type, wait, caption, term.box, term.clear, term.enter, term.paste, term.print, term.prompt, term.type, term.work)',
    'beats[2]: when "sideways" names no take (wide, tall, full, brisk)',
    "beats[3]: the film has no value @nowhere",
    'beats[4]: the film has no hook "missing"',
    'beats[6].beats[0] starts with "term.dance", which is no action (address, begin, camera, click, do, endCard, end, focus, follow, gone, hide, highlight, hold, look, mark, move, open, raise, read, ready, say, scroll, scrollPage, show, still, type, wait, caption, term.box, term.clear, term.enter, term.paste, term.print, term.prompt, term.type, term.work)',
    "beats[7].morph[0]: the film has no value @nowhere",
    "beats[8] is not a beat",
  ]);
  assert.throws(() => player({beats: [{hodl: 1}]}), /The film can't play: beats\[0\] starts with "hodl"/);
  assert.throws(() => player({}), /neither beats nor play/);
  const play = async () => {};
  assert.equal(player({play}), play);
});

test("both of the repository's films are plain data with nothing missing", async () => {
  for (const name of ["people-requests", "accelerator-demo"]) {
    const film = await import(`./film/films/${name}.js`);
    const sets = film.scenes ? film.scenes.map(scene => [scene.name, scene.beats]) : [["beats", film.beats]];
    for (const [where, beats] of sets) {
      assert.deepEqual(check(beats, film.values, where), [], name);
      assert.deepEqual(JSON.parse(JSON.stringify(beats)), beats, `${name} ${where} holds only data`);
    }
  }
});
