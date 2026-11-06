// A take checked as mographc checks a Remotion film (web/film/vendor/mographc, MIT): the probe's
// rows become its film model, and its rules report what a viewer would notice, each finding with
// what was measured, what the rule requires and how to fix it.
//
// Each frame is measured twice (probe.js), and each measurement gets the rules that hold for it:
// - a window's own page, where only the product moves things: what jumps, flickers, pops in or
//   out, or stops dead is the product's motion, whatever the camera did;
// - the picture, as the viewer sees it: words cut by the frame, two windows seen through each
//   other, the cursor arriving and leaving, and the key frames.
// Rules made for motion graphics stay off: a filmed product holds still while it's read, its
// cursor is the system's, and its cells truncate long text by design. A desk film is checked
// scene by scene, so a long take never holds every frame's rows at once.
import {allRules} from "./vendor/mographc/config.js";
import {Film, area} from "./vendor/mographc/film.js";
import {run} from "./vendor/mographc/lint.js";

const PAGE_RULES = {
  "continuity/jump": "error",
  "continuity/flicker": "error",
  "animate/pop-in": "warn",
  "animate/pop-out": "warn",
  "assets/missing-image": "error",
  "assets/fallback-font": "error",
  "easing/abrupt-stop": "warn",
  "easing/stutter": "warn",
};
const PICTURE_RULES = {
  "layout/text-cut": "error",
  "staging/double-exposure": "error",
  "cursor/low-contrast": "warn",
  "film/mark-settled": "error",
  "film/mark-cursor-on-text": "warn",
};

// A stage (a sketch, or a staged film's page) is drawn to be filmed: its type, its spacing and
// where its parts sit are the film's own, so the motion-graphics layout rules hold for it as they
// do for a Remotion film. Words on words, a headline half over a card, and parts left off the
// canvas are errors: a founder would see them.
export const STAGE_RULES = {
  "layout/text-overlap": "error",
  "staging/text-too-small": "error",
  // A hero runs off an edge on purpose, and its words near that edge are framed, not crowded.
  "staging/no-padding": "warn",
  "staging/text-over-product": "error",
  "layout/subject-off-canvas": "error",
  "layout/unbalanced-space": "warn",
  "staging/title-off-centre": "warn",
  "staging/top-banner": "warn",
};

const changed = (a, b) => {
  if (!a || !b) return Boolean(a || b);
  const moved = Math.hypot(a.box[0] - b.box[0], a.box[1] - b.box[1]);
  const resized = Math.abs(a.box[2] - b.box[2]) + Math.abs(a.box[3] - b.box[3]);
  return moved > 0.5 || resized > 1 || Math.abs(a.opacity - b.opacity) > 0.02 || a.raw.x !== b.raw.x;
};

// Our own rules, in mographc's rule format: what the fresh-eye review of the accelerator demo
// caught by eye.
const markSettled = {
  id: "film/mark-settled",
  kind: "lint",
  severity: "error",
  options: {marks: []},
  docs: {
    summary: "A key frame is taken once the picture has settled: nothing on it moved, resized, faded or changed its words in the frame before.",
    why: "Key frames become the poster and the review sheet. One taken while something is still moving shows a half-faded toast or a panel halfway in, which reads as a broken product.",
    fix: "Hold until the change has finished, then mark the frame: ready() for what arrives, gone() for what leaves, or a short hold.",
    options: {marks: "The key frames, as [name, frame] pairs, set by the recorder."},
  },
  check({film, options, report}) {
    for (const [name, frame] of options.marks) {
      if (!film.byFrame.has(frame) || !film.byFrame.has(frame - 1)) continue;
      // The film's own highlight fades as written, and a spinner or a caret never stops; anything
      // else still changing is caught mid-way.
      const ambient = el => {
        let busy = 0;
        for (let n = frame - 30; n < frame; n++) if (changed(el.at(n - 1), el.at(n))) busy++;
        return busy >= 20;
      };
      const moving = film.elements.filter(el => el.role !== "cursor" && !el.at(frame)?.raw.film && area(el.at(frame)?.visible ?? [0, 0, 0, 0]) > 0 && changed(el.at(frame - 1), el.at(frame)) && !ambient(el));
      if (!moving.length) continue;
      report({element: moving[0], frames: [frame - 1, frame], measured: `the key frame "${name}" was taken while ${moving.length} element${moving.length === 1 ? " was" : "s were"} still changing, ${moving[0].label} among them`, need: "a key frame taken after what changed has settled"});
    }
  },
};

const markCursor = {
  id: "film/mark-cursor-on-text",
  kind: "lint",
  severity: "warn",
  options: {marks: []},
  docs: {
    summary: "At a key frame the cursor's tip rests on empty surface, not on words.",
    why: "A key frame is read as a still; a cursor over words hides them and looks like a pointer left behind.",
    fix: "After the last click before a key frame, move the hand to empty surface near it (BLANK), then mark.",
    options: {marks: "The key frames, as [name, frame] pairs, set by the recorder."},
  },
  check({film, options, report}) {
    for (const [name, frame] of options.marks) {
      const cursor = film.elements.find(el => el.role === "cursor" && el.at(frame)?.opacity > 0.3);
      const tip = cursor?.at(frame)?.raw.tp;
      if (!tip) continue;
      const word = film.elements.find(el => el.role !== "cursor" && el.at(frame)?.raw.x && el.at(frame).opacity > 0.3 && within(tip, el.at(frame).visible));
      if (word) report({element: cursor, frames: [frame, frame], measured: `at the key frame "${name}" its tip rests on ${word.label}`, need: "the tip on empty surface at a key frame"});
    }
  },
};

const within = ([x, y], box) => x >= box[0] && x <= box[0] + box[2] && y >= box[1] && y <= box[1] + box[3];

// A config that turns every rule off but the ones named, at the severity named.
function only(rules, options = {}) {
  const custom = [markSettled, markCursor];
  const off = Object.fromEntries(allRules().map(rule => [rule.id, "off"]));
  const on = Object.fromEntries(Object.entries(rules).map(([id, severity]) => [id, options[id] ? [severity, options[id]] : severity]));
  return [{name: "product-film", plugins: [...allRules(), ...custom], rules: {...off, ...on}}];
}

function filmOf(name, fps, frames, offset) {
  const [width, height] = frames[0].c;
  return new Film({composition: name, fps, width, height, durationInFrames: frames.length, frames: frames.map((frame, index) => ({...frame, f: index}))});
}

function shifted(findings, fps, offset, where) {
  return findings.map(finding => ({
    ...finding,
    where,
    from: Math.round((finding.from + offset / fps) * 100) / 100,
    to: Math.round((finding.to + offset / fps) * 100) / 100,
    frames: finding.frames.map(frame => frame + offset),
  }));
}

// `measured` holds probe.js's measurements for one stretch of the take, from frame `offset`;
// `marks` maps a key frame's name to its frame in the take; `accepts` lists findings the film
// means to have, as {rule, frames: [first, last], reason} in the take's frames. A `staged` take
// is held to the stage's layout rules too.
export function checkFrames(measured, {name, fps, offset = 0, marks = {}, accepts = [], staged = false}) {
  if (!measured.length) return [];
  const findings = [];
  const inStretch = Object.entries(marks).filter(([, frame]) => frame >= offset && frame < offset + measured.length).map(([mark, frame]) => [mark, frame - offset]);
  const picture = filmOf(name, fps, measured.map(item => item.picture), offset);
  const markOptions = {marks: inStretch};
  findings.push(...shifted(run(picture, only(staged ? {...PICTURE_RULES, ...STAGE_RULES} : PICTURE_RULES, {"film/mark-settled": markOptions, "film/mark-cursor-on-text": markOptions})), fps, offset, "picture"));
  // Each window's page, over each run of frames in which it shows.
  const ids = new Set(measured.flatMap(item => Object.keys(item.pages)));
  for (const id of ids) {
    let start = null;
    for (let i = 0; i <= measured.length; i++) {
      const shows = i < measured.length && measured[i].pages[id];
      if (shows && start === null) start = i;
      if (!shows && start !== null) {
        const stretch = measured.slice(start, i).map(item => item.pages[id]);
        if (stretch.length > 1) {
          const page = filmOf(`${name} ${id}`, fps, stretch);
          findings.push(...shifted(run(page, only(PAGE_RULES)).filter(item => entrance(item) && !layout(item, page)), fps, offset + start, id));
        }
        start = null;
      }
    }
  }
  for (const finding of findings) {
    const why = accepts.find(item => item.rule === finding.rule && finding.frames[0] >= item.frames[0] && finding.frames[1] <= item.frames[1] + 1);
    if (why && !finding.accepted) finding.accepted = why.reason;
  }
  return findings.sort((a, b) => a.from - b.from || a.rule.localeCompare(b.rule));
}

// The findings for the console: each one that isn't accepted, and a count for each accepted reason.
export function summary(findings) {
  // One line for each rule, place and moment; many elements caught at once are one change.
  const groups = new Map();
  for (const item of findings.filter(item => !item.accepted)) {
    const key = `${item.rule} ${item.where} ${item.frames.join("-")}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const open = [...groups.values()].map(([first, ...rest]) => `${describe(first)}${rest.length ? ` (and ${rest.length} more at the same moment)` : ""}`);
  const accepted = new Map();
  for (const item of findings.filter(item => item.accepted)) accepted.set(item.accepted, (accepted.get(item.accepted) ?? 0) + 1);
  return [...open, ...[...accepted].map(([reason, count]) => `${count} accepted: ${reason}`)];
}

// A panel, card, toast or line of text that pops in or out reads as a glitch; a small box changing
// its background in one frame (a hovered row, a ticked box) is the product changing state.
const entrance = finding => !(finding.rule.startsWith("animate/pop-") && finding.element?.startsWith("graphic "));

// A container as wide as the page resizes with whatever a route shows; a real jump in it shows in
// the words and controls it holds, which are checked on their own.
const layout = (finding, page) => {
  if (finding.rule !== "continuity/jump") return false;
  const size = finding.element?.match(/ (\d+)x(\d+) at /);
  return Boolean(size) && Number(size[1]) >= page.width * 0.9;
};

// One line for the report: when, where, which rule, on what, what was measured and what it needs.
export function describe(finding) {
  const when = finding.from === finding.to ? `${finding.from.toFixed(2)}s` : `${finding.from.toFixed(2)}-${finding.to.toFixed(2)}s`;
  const accepted = finding.accepted ? ` (accepted: ${finding.accepted})` : "";
  return `${when} [${finding.where}] ${finding.rule} ${finding.element ?? ""}: ${finding.measured}. Required: ${finding.need}${accepted}`;
}
