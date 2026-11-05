// Films a feature: Tin's real dashboard, served on a demo world (web/app-fixture.js), filmed one
// frame at a time under a virtual clock with a directed camera, cursor and captions. Writes an MP4,
// a GIF, a poster, a contact sheet of the marks and captions per format. See docs/feature-films.md.
//
//   npm run film -- people-requests [--format 4:5,16:9] [--theme dark|light] [--out .films] [--fonts <stylesheet url>]
import {spawn} from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {pathToFileURL} from "node:url";
import {chromium} from "playwright";
import {checkFrames, describe, summary} from "./checks.js";
import {measureFrame} from "./probe.js";
import {findSecrets, openRecording} from "./recording.js";
import {player} from "./spec.js";
import {serveApp} from "../app-fixture.js";
import {installVirtualClock} from "./clock.js";
import {bezier} from "./ease.js";
import {installScreen, installStage} from "./overlay.js";

export const FPS = 30;
// Output sizes, the caption band under the picture, and how wide a screen the dashboard is filmed
// on. 4:5 for LinkedIn and X feeds, 16:9 for X on desktop, the site and blog posts. The tall
// formats use a narrower screen, so the dashboard shows its own compact layouts.
export const FORMATS = {
  "4:5": {width: 1080, height: 1350, band: 210, screen: 720},
  "16:9": {width: 1920, height: 1080, band: 160, screen: 1440},
  "1:1": {width: 1080, height: 1080, band: 180, screen: 720},
};

// The screen is exactly the picture when the camera is all the way out; the window is a band's
// height taller, so the band never covers the dashboard.
// A film can pick its own screen per format: a width, or {width, height, margin} where a taller
// screen than the picture lets the camera leave chrome (a top menu) out of the frame, and a margin
// sets the screen as a window on the film's backdrop, as Screen Studio does, so the camera can
// frame past its edge. The room is the window and its margin: everything the camera may show.
// A margin is a number for every side, or {top, right, bottom, left}.
export function stage(size, choice = size.screen) {
  const width = typeof choice === "object" ? choice.width : choice;
  const given = (typeof choice === "object" && choice.margin) || 0;
  const margin = typeof given === "number" ? {top: given, right: given, bottom: given, left: given} : {top: 0, right: 0, bottom: 0, left: 0, ...given};
  const height = typeof choice === "object" ? choice.height : Math.round((size.height - size.band) * (width / size.width));
  const room = {width: width + margin.left + margin.right, height: height + margin.top + margin.bottom};
  return {screen: {width, height}, margin, room, viewport: {width: room.width, height: Math.ceil(room.height + size.band * (room.width / size.width))}};
}

// What the screen sits on: black under the dark theme, white under the light one.
export const BACKDROP = {dark: "#000", light: "#fff"};

// The member's picture in the sidebar, as Clerk gives every account one: their initial on a
// muted ground. Without it the dashboard shows an empty circle where the picture goes.
export function avatar(name) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#6b6f5a"/><text x="32" y="42" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="28" fill="#f4f1ea">${name.slice(0, 1)}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
// The smallest text a frame may show, as a share of its short side: about 22px at 1080.
const SMALLEST_TEXT = 0.02;
// The cuts' dip and the cursor's press; everything else uses the named curves in ease.js.
const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
// A hand's reach (Flash and Hogan, 1985): the path that changes acceleration least, so its speed
// rises and falls as one smooth bell with no kink at the middle, as a person's pointing does.
const reach = t => t * t * t * (10 - 15 * t + 6 * t * t);
const lerp = (a, b, t) => a + (b - a) * t;
const seconds = s => Math.max(1, Math.round(s * FPS));
// Stageshow's cursor timing (kit/cursor.css): the press, its release, and the ring a click can
// send out, 0.55 s on its own curve.
const PRESS_IN = bezier([0, 0, 0.58, 1]);
const LET_GO = bezier([0.22, 1, 0.36, 1]);
const RING = Math.round(0.55 * FPS);
const RING_CURVE = bezier([0.25, 0.8, 0.25, 1]);

// Where a camera's path creeps: a run of frames that each move it a little, but less than
// `CREEP` output px, longer than a third of a second. A move goes somewhere or holds still.
const CREEP = 1.2;
export function creeps(lens, out) {
  const found = [];
  let run = null;
  for (let n = 1; n < lens.length; n++) {
    const [x, y, width] = lens[n], [px, py, pwidth] = lens[n - 1];
    const k = out / width;
    const moved = Math.max(Math.hypot(x - px, y - py) * k, Math.abs(width - pwidth) * k);
    const small = moved > 0.05 && moved < CREEP;
    if (small) run = run || {at: n, frames: 0};
    if (small) run.frames++;
    if (!small && run) {
      if (run.frames > FPS / 3) found.push(run);
      run = null;
    }
  }
  if (run && run.frames > FPS / 3) found.push(run);
  return found;
}

// Where a camera's path ([centre x, centre y, width] a frame) sets off one way and comes back
// within two seconds, by more than a tenth of its frame each way: a zig-zag.
export function zigzags(lens) {
  const found = [];
  for (const [axis, name] of [[0, "side to side"], [1, "up and down"]]) {
    // Runs of movement in one direction: where each starts and how far it goes.
    const runs = [];
    for (let n = 1; n < lens.length; n++) {
      const step = lens[n][axis] - lens[n - 1][axis];
      if (Math.abs(step) < 0.2) continue;
      const last = runs.at(-1);
      if (last?.sign === Math.sign(step)) last.travel += Math.abs(step);
      else runs.push({sign: Math.sign(step), start: n, travel: Math.abs(step), width: lens[n][2]});
    }
    for (const [index, there] of runs.entries()) {
      const back = runs[index + 1];
      if (back && there.travel > there.width * 0.1 && back.travel > there.width * 0.1 && back.start - there.start < 2 * FPS) {
        found.push({axis: name, at: there.start, there: Math.round(there.travel), back: Math.round(back.travel)});
      }
    }
  }
  return found;
}

export class Director {
  // `page` is the stage, `app` the dashboard's frame in it. Everything the director works in
  // (frames, the cursor, rects) is in the stage's px; the screen sits `margin` in from its corner.
  constructor({page, app, format, size, screen, margin = {top: 0, right: 0, bottom: 0, left: 0}, room = screen, deviceScale, served, issues, ripple = false, check = false}) {
    Object.assign(this, {page, app, format, size, screen, margin, room, deviceScale, world: served, issues, ripple});
    // With `check`, every frame is measured (probe.js) and checked (checks.js) as the take goes.
    this.checking = check;
    this.measured = [];
    this.findings = [];
    // Frames shot while the camera followed the hand: it crops through the screen as it goes.
    this.followed = [];
    // Where each frame's time goes (ms by stage), for the report: a slow scene says why.
    this.timing = {};
    this.wide = size.width > size.height;
    this.n = 0;
    this.recording = true;
    this.tracks = new Set();
    this.captions = [];
    this.ripples = [];
    this.marks = {};
    this.hashes = [];
    this.trail = [];
    this.lens = [];
    // Every trip the hand makes, for the path check.
    this.journeys = [];
    this.cursor = {x: 0, y: 0, visible: false, pressAt: null, releaseAt: null};
    // What moved when, for the camera rules and the motion strips.
    this.moves = [];
    this.events = [];
    this.cameraMoving = false;
    this.fade = 0;
    this.ending = 0;
    this.highlights = [];
    this.travelling = false;
    this.follower = null;
    // Text under the smallest size, by what it says: the least size and the marks it shows at.
    this.small = new Map();
    this.inside = null;
    // CSS px of the stage per CSS px of the window the director works in (a desk scales its windows).
    this.windowScale = 1;
    this.frame = {x: 0, y: 0, width: screen.width, height: screen.width * size.height / size.width};
    this.last = null;
  }

  // A box between the screen's px and the stage's.
  onStage(box) { return box && {...box, x: box.x + this.margin.left, y: box.y + this.margin.top}; }
  onScreen(box) { return box && {...box, x: box.x - this.margin.left, y: box.y - this.margin.top}; }

  // CSS px of page per output px, and the part of the frame above the caption band.
  get scale() { return this.frame.width / this.size.width; }
  get picture() { return {...this.frame, height: (this.size.height - this.size.band) * this.scale}; }

  // ---- what a film script calls ----

  async open(route, {ready} = {}) {
    await this.app.evaluate(to => {
      history.pushState(null, "", to);
      dispatchEvent(new PopStateEvent("popstate"));
    }, `${route}${route.includes("?") ? "&" : "?"}project=project`);
    if (ready) await this.ready(ready);
  }

  // Frames go by (recorded) until something is visible, or until it has gone.
  async ready(selector, {within = 10} = {}) {
    for (let i = 0; i < within * FPS; i++) {
      if (await this.app.locator(selector).first().isVisible()) return;
      await this.shoot();
    }
    throw new Error(`Nothing matched ${selector} within ${within}s.`);
  }

  async gone(selector, {within = 10} = {}) {
    for (let i = 0; i < within * FPS; i++) {
      if (!(await this.app.locator(selector).count())) return;
      await this.shoot();
    }
    throw new Error(`${selector} was still there after ${within}s.`);
  }

  // Frame a target: everything it covers (with `text`, only its words), plus `pad` CSS px, as
  // large as the format allows. It never zooms past the device scale, where every output pixel
  // is still a pixel the browser drew.
  // `inside` (the director's default, or per call) keeps the frame within a region where it can,
  // so a close-up never ends halfway across a sidebar. After Stageshow's Camera: 1.1 s on its own
  // curve, and the report holds the film to one push-in, within 2x, with the cursor still.
  // The crop never cuts a word or a control: the frame moves by up to `nudge` CSS px to a clean
  // line, never above `floor`.
  async camera(target, {pad = 28, duration = 1.1, curve = "camera", cut = false, wait = true, align = "center", valign = "center", text = false, inside = this.inside, nudge = 24, floor = 0} = {}) {
    const box = await this.rect(target, text);
    const {width, height, band} = this.size;
    const room = height - band;
    let scale = Math.max((box.width + pad * 2) / width, (box.height + pad * 2) / room, 1 / this.deviceScale);
    scale = Math.min(scale, this.room.width / width, this.room.height / room);
    const frame = {width: width * scale, height: height * scale};
    const left = align === "left" ? box.x - pad : align === "right" ? box.x + box.width + pad - frame.width : box.x + box.width / 2 - frame.width / 2;
    frame.x = Math.min(Math.max(0, left), this.room.width - frame.width);
    const region = inside && this.onStage(await this.app.evaluate(selector => window.__film.rect(selector), inside));
    if (region && frame.width <= region.width) frame.x = Math.min(Math.max(region.x, frame.x), region.x + region.width - frame.width);
    const top = valign === "top" ? box.y - pad : box.y + box.height / 2 - (room * scale) / 2;
    frame.y = Math.max(0, Math.min(top, this.room.height - room * scale));
    if (nudge) {
      const top = Math.max(0, floor);
      const shift = await this.app.evaluate(([frame, picture, room, nudge]) => window.__film.clean(frame, picture, room, nudge),
        [this.onScreen(frame), room * scale, this.onScreen({x: 0, y: top, width: this.room.width, height: this.room.height - top}), nudge]);
      frame.x += shift.dx;
      frame.y += shift.dy;
    }
    if ((box.width + pad * 2) / width > scale + 1e-6 || (box.height + pad * 2) / room > scale + 1e-6) {
      this.warn(`The camera can't fit ${JSON.stringify(target)} in ${this.format}; it is cut.`);
    }
    if (cut || !this.n) {
      this.frame = frame;
      // A click's ring belongs to the shot it was pressed in.
      this.ripples = [];
      return;
    }
    const from = {...this.frame};
    const frames = seconds(duration);
    const curveAt = bezier(curve);
    const zoom = from.width / frame.width;
    if (Math.abs(zoom - 1) < 0.02 && Math.hypot(frame.x - from.x, frame.y - from.y) < 2) return;
    this.moves.push({label: JSON.stringify(target).slice(0, 60), start: this.n, end: this.n + frames, zoom, seconds: frames / FPS});
    this.events.push({label: `camera ${zoom > 1.02 ? "in" : zoom < 0.98 ? "out" : "pan"}`, start: this.n, end: this.n + frames});
    let i = 0;
    this.cameraMoving = true;
    const track = () => {
      i++;
      const t = curveAt(i / frames);
      this.frame = {x: lerp(from.x, frame.x, t), y: lerp(from.y, frame.y, t), width: lerp(from.width, frame.width, t), height: lerp(from.height, frame.height, t)};
      if (i >= frames) this.cameraMoving = false;
      return i >= frames;
    };
    await this.run(track, wait);
  }

  // The cursor travels in a shallow arc, as a hand does (after Stageshow's act.move). The page
  // sees the pointer only where the hand stops: it leaves as the trip starts and arrives with it,
  // so a hover ends when the hand moves off and nothing it passes over lights up.
  async move(target, {to = [0.5, 0.5], duration} = {}) {
    if (this.cameraMoving) this.warn(`The cursor moves toward ${JSON.stringify(target)} while the camera moves; Stageshow holds the cursor still while the view moves.`);
    const box = await this.rect(target);
    const end = {x: box.x + box.width * to[0], y: box.y + box.height * to[1]};
    // It comes in from the right edge, level with where it's going, so it crosses nothing that
    // would light up on the way.
    if (!this.cursor.visible) {
      const picture = this.picture;
      Object.assign(this.cursor, {visible: true, x: picture.x + picture.width * 1.03, y: end.y + picture.height * 0.03});
    }
    const from = {x: this.cursor.x, y: this.cursor.y};
    // Stageshow's hand: a shallow arc, quicker over short hops (distance in screen px).
    const distance = Math.hypot(end.x - from.x, end.y - from.y);
    const frames = seconds(duration ?? Math.min(0.65, Math.max(0.3, (260 + distance * 0.9) / 1000)));
    const bow = Math.min(distance * 0.08, 24);
    const length = Math.hypot(end.x - from.x, end.y - from.y) || 1;
    const normal = {x: -(end.y - from.y) / length, y: (end.x - from.x) / length};
    let i = 0;
    this.travelling = true;
    this.aim = box;
    this.journeys.push({from, to: end, start: this.n, end: this.n + frames, click: false, label: JSON.stringify(target).slice(0, 60)});
    this.goal = end;
    await this.run(() => {
      i++;
      const t = reach(i / frames);
      const lift = Math.sin(Math.PI * t) * bow;
      this.cursor.x = lerp(from.x, end.x, t) + normal.x * lift;
      this.cursor.y = lerp(from.y, end.y, t) + normal.y * lift;
      if (i >= frames) this.travelling = false;
      return i >= frames;
    });
    this.within(end, target);
  }

  // Move, rest a moment, press: the drawn cursor dips (in fast, let go softly, as Stageshow's
  // does) and the page gets a real click at the same point. A film that asks for it (`ripple`)
  // sends a soft ring out too, for when a click must read at a glance.
  async click(target, options) {
    await this.move(target, options);
    this.journeys.at(-1).click = true;
    this.events.push({label: "click", start: this.n - 4, end: this.n + 14});
    await this.hold(0.12);
    const before = typeof target === "object" && !Array.isArray(target) ? null : await this.app.evaluate(selectors => window.__film.rect(selectors), target);
    this.cursor.pressAt = this.n;
    await this.page.mouse.down();
    const ripple = {x: this.cursor.x, y: this.cursor.y, start: this.n, gone: null};
    if (this.ripple) this.ripples.push(ripple);
    await this.hold(0.1);
    await this.page.mouse.up();
    this.cursor.releaseAt = this.n;
    // The ring ends with what was pressed: once it is gone or covered at the press, it fades out.
    if (this.ripple && (typeof target === "string" || Array.isArray(target))) this.tracks.add(async () => {
      if (ripple.gone !== null || this.n - ripple.start > RING) return true;
      const there = await this.app.evaluate(([selectors, x, y]) => window.__film.hits(selectors, x, y), [target, ripple.x - this.margin.left, ripple.y - this.margin.top]);
      if (!there) ripple.gone = this.n;
      return !there;
    });
    // The page shouldn't move what was just clicked out from under the cursor. Checked a few
    // frames on, as the next calls go.
    let frames = 0;
    this.tracks.add(async () => {
      if (++frames < 6) return false;
      const after = before && (await this.app.evaluate(selectors => window.__film.rect(selectors), target));
      const moved = after ? Math.round(Math.hypot(after.x - before.x, after.y - before.y)) : 0;
      if (moved > 4) this.warn(`Clicking ${JSON.stringify(target)} moved it ${moved}px: the page shifted under the cursor.`);
      return true;
    });
  }

  // Scroll a container until a target inside it sits `offset` CSS px below its top edge.
  async scroll(container, target, {duration = 0.8, offset = 24} = {}) {
    const [from, to] = await this.app.evaluate(([outer, inner, gap]) => {
      const box = document.querySelector(outer);
      const top = box.scrollTop + document.querySelector(inner).getBoundingClientRect().top - box.getBoundingClientRect().top - gap;
      return [box.scrollTop, Math.max(0, Math.min(top, box.scrollHeight - box.clientHeight))];
    }, [container, target, offset]);
    const frames = seconds(duration);
    let i = 0;
    await this.run(async () => {
      i++;
      await this.app.evaluate(([outer, top]) => (document.querySelector(outer).scrollTop = top), [container, lerp(from, to, ease(i / frames))]);
      return i >= frames;
    });
  }

  async type(text, {interval = 0.06} = {}) {
    for (const character of text) {
      await this.page.keyboard.type(character);
      await this.hold(interval);
    }
  }

  // One caption at a time: the old one fades out, the new one fades in and rises a little, `delay`
  // frames on (a cut hands its caption over while the frame is dark).
  caption(text, {delay = 0} = {}) {
    const current = this.captions.at(-1);
    let start = this.n + delay;
    if (current && current.end === null) {
      current.end = this.n + 5;
      start = Math.max(current.end, start);
    }
    if (text) this.captions.push({text, start, end: null});
  }

  async hold(duration) {
    for (let i = 0, frames = seconds(duration); i < frames; i++) await this.shoot();
  }

  // A cut behind a fade: what `change` does (open another view, move the world on, let time pass)
  // isn't filmed. It accelerates into the cut and settles out of it (Stageshow's through and settle
  // curves); a new caption comes in with the new scene.
  async cut(change, {fade = 0.25, caption} = {}) {
    let i = 0;
    const frames = seconds(fade);
    if (caption !== undefined) this.caption(caption, {delay: frames});
    this.events.push({label: "cut", start: this.n - 4, end: this.n + frames * 2 + 4});
    // An even dip, eased both ways, with no hold at the bottom.
    await this.run(() => (this.fade = ease(++i / frames)) >= 1);
    this.recording = false;
    await change();
    // What was marked before the cut belongs to the picture that went.
    this.highlights = this.highlights.filter(mark => mark.start > this.n);
    this.recording = true;
    i = 0;
    await this.run(() => (this.fade = 1 - ease(++i / frames)) <= 0);
  }

  // A change shown where it happens, with no cut: `change` moves the world on and has the app
  // re-render, unfilmed; then what differs on screen rolls, slides, arrives or fades (`morph` in
  // overlay.js). `caption` arrives with it.
  async morph(change, {caption} = {}) {
    await this.app.evaluate(() => window.__film.capture());
    this.recording = false;
    await change();
    this.recording = true;
    const done = await this.app.evaluate(() => window.__film.morph());

    this.events.push({label: "morph", start: this.n - 2, end: this.n + 24});
    if (caption !== undefined) this.caption(caption);
    return done;
  }

  // Text too small to read on a phone, said once for the film, smallest first.
  judgeText() {
    if (!this.small.size) return;
    const floor = Math.round(SMALLEST_TEXT * Math.min(this.size.width, this.size.height));
    const runs = [...this.small].sort((a, b) => a[1].size - b[1].size);
    const marks = [...new Set(runs.flatMap(([, item]) => [...item.marks]))];
    this.warn(`${runs.length} text runs are under ${floor}px (at ${marks.join(", ")}): ${runs.slice(0, 5).map(([text, item]) => `“${text}” ${item.size}px`).join(", ")}.`);
  }

  // A hand goes where it's going. A trip that only steps aside and then comes most of the way back
  // (from a plain move, not a press) reads as fidgeting: say so.
  judgeHand() {
    const far = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    for (const [index, first] of this.journeys.entries()) {
      const next = this.journeys[index + 1];
      if (!next || first.click || next.start - first.end > 4 * FPS) continue;
      const travelled = far(first.from, first.to) + far(next.from, next.to);
      const direct = far(first.from, next.to);
      if (travelled > direct * 1.4 && travelled - direct > 80) {
        this.warn(`The hand steps aside to ${first.label} and comes back toward ${next.label}, ${Math.round(travelled - direct)}px out of its way; go straight, or let it rest where it was.`);
      }
    }
  }

  judgeLens() {
    for (const creep of creeps(this.lens, this.size.width)) {
      this.warn(`The camera creeps at ${(creep.at / FPS).toFixed(1)}s: ${creep.frames} frames in a row move it under ${CREEP} output px each; a move should end still.`);
    }
    for (const zig of zigzags(this.lens)) {
      this.warn(`The camera zig-zags ${zig.axis} at ${(zig.at / FPS).toFixed(1)}s: ${zig.there}px one way, then ${zig.back}px back.`);
    }
  }

  // Stageshow's camera rules, read once the film has played.
  judgeCamera() {
    const pushes = this.moves.filter(move => move.zoom > 1.02);
    for (const move of this.moves) {
      if (move.zoom > 2.01) this.warn(`The camera zooms ${move.zoom.toFixed(1)}x in one move (${move.label}); a push-in stays within 2x.`);
      if (move.zoom > 1.15 && move.seconds < 1.1) this.warn(`The camera zooms ${move.zoom.toFixed(1)}x in ${move.seconds.toFixed(1)}s (${move.label}); a push-in takes 1.1s or more.`);
    }
    if (pushes.length > 1) this.warn(`The camera pushes in ${pushes.length} times; one push-in per story, in and back out.`);
    if (this.moves.length > 3) this.warn(`The camera moves ${this.moves.length} times; cut any move nobody would miss.`);
  }

  hide() { this.cursor.visible = false; }

  // Screen Studio's follow camera. From wherever the camera is, it eases in to `zoom` (of the
  // frame it starts from) and keeps centring on what matters: the cursor, every mark still
  // showing, and whatever matches `attention` (a toast, say), zooming out only as far as they
  // need to fit. It moves in glides: each one eased, 0.7 to 1.2 s by how far it goes, setting
  // off at the speed the camera already has and ending exactly still, as an operator's move
  // does. A camera that creeps on, a pixel at a time, after the move reads as jitter. Near the
  // window's edge it keeps centring, up to `overshoot` of the frame past the edge, where the
  // window's boundary shows on the backdrop.
  follow({zoom = 1.6, attention = [], pad = 48, overshoot = 0.3} = {}) {
    const frame = this.frame;
    const picture = this.picture;
    const at = {x: picture.x + picture.width / 2, y: picture.y + picture.height / 2, width: frame.width};
    this.follower = {zoom, attention, pad, overshoot, widest: frame.width, target: null, at, speed: {x: 0, y: 0, width: 0}, glide: null};
  }

  // Back to a camera that moves only when told.
  still() { this.follower = null; }

  // Tell a following camera what else it must keep in frame for now, as a director frames a beat
  // before it happens: say, where a panel is about to open. Rects or selectors; nothing to clear.
  async look(targets) {
    if (!this.follower) return;
    this.follower.look = targets ? await Promise.all([].concat(targets).map(target => this.rect(target))) : [];
  }

  async track() {
    const follow = this.follower;
    const {width: out, height: tall, band} = this.size;
    const ratio = (tall - band) / out;
    const union = boxes => {
      if (!boxes.length) return null;
      const left = Math.min(...boxes.map(box => box.x)), top = Math.min(...boxes.map(box => box.y));
      return {x: left, y: top, width: Math.max(...boxes.map(box => box.x + box.width)) - left, height: Math.max(...boxes.map(box => box.y + box.height)) - top};
    };
    // What must show: the hand, and anything the film named (a toast, say). What it would like to
    // show: every mark still on screen, when it fits beside them at this zoom. A film knows where
    // the hand is going, so on a trip the camera looks at where it will land, as an operator who
    // knows the script would, and never chases it out and back.
    const must = [];
    const hand = this.travelling && this.goal ? this.goal : this.cursor;
    if (this.cursor.visible) must.push({x: hand.x - 20, y: hand.y - 20, width: 40, height: 40});
    must.push(...(follow.look || []));
    if (follow.attention.length) {
      // Where each lands, from its layout, ignoring transforms (Stageshow's rect with `layout`):
      // a panel sliding in or a toast rising is framed where it will be, never chased.
      const seen = await this.app.evaluate(selectors => selectors.map(selector => {
        const element = document.querySelector(selector);
        if (!element?.offsetWidth || getComputedStyle(element).visibility === "hidden") return null;
        let x = 0, y = 0;
        for (let node = element; node; node = node.offsetParent) {
          x += node.offsetLeft;
          y += node.offsetTop;
        }
        return {x, y, width: element.offsetWidth, height: element.offsetHeight};
      }), follow.attention);
      must.push(...seen.filter(Boolean).map(box => this.onStage(box)));
    }
    const marks = this.highlights.filter(mark => mark.start <= this.n + 1 && this.n - mark.start < mark.hold + 23).map(mark => mark.box);
    const close = follow.widest / follow.zoom;
    const fits = (box, width) => box.width + follow.pad * 2 <= width && box.height + follow.pad * 2 <= width * ratio;
    const needed = union(must);
    let width = close;
    let focus = needed;
    if (needed) {
      // One zoom: it opens out only as far as what must show needs.
      width = Math.min(follow.widest, Math.max(close, needed.width + follow.pad * 2, (needed.height + follow.pad * 2) / ratio));
      const wider = union([...must, ...marks]);
      if (marks.length && fits(wider, width)) focus = wider;
    }
    // Whether to re-aim, as an operator decides: to bring something into view, never because
    // something left. The camera moves when the hand's landing point leaves the middle of the
    // frame it is heading for, when something it must show leaves that frame, or when a new mark
    // appears outside it and fits; a mark fading or a frame that could be a little tighter never
    // moves it, so there are no micro-adjustments.
    const keys = ["x", "y", "width"];
    let target = follow.target;
    const inside = (box, frame, inset) => {
      const high = frame.width * ratio;
      const left = frame.x - frame.width / 2 + frame.width * inset, top = frame.y - high / 2 + high * inset;
      return box.x >= left && box.y >= top && box.x + box.width <= frame.x + frame.width / 2 - frame.width * inset && box.y + box.height <= frame.y + high / 2 - high * inset;
    };
    if (focus) {
      const handBox = must[0];
      const marked = marks.length ? union([...must, ...marks]) : null;
      const go = !target
        || (handBox && !inside(handBox, target, 0.18))
        || (needed && !inside(needed, target, 0.02))
        || (marked && focus === marked && !inside(marked, target, 0.02))
        || Math.abs(width - target.width) / target.width > 0.06;
      if (go) {
        const center = {x: focus.x + focus.width / 2, y: focus.y + focus.height / 2};
        // Past the window's edge by no more than the overshoot, and never out of the room.
        const screen = {x: this.margin.left, y: this.margin.top, width: this.screen.width, height: this.screen.height};
        const high = width * ratio;
        const spare = {x: follow.overshoot * width, y: follow.overshoot * high};
        const keep = (value, low, highest) => (low > highest ? (low + highest) / 2 : Math.min(Math.max(value, low), highest));
        const next = {
          x: keep(center.x, Math.max(screen.x - spare.x, 0) + width / 2, Math.min(screen.x + screen.width + spare.x, this.room.width) - width / 2),
          y: keep(center.y, Math.max(screen.y - spare.y, 0) + high / 2, Math.min(screen.y + screen.height + spare.y, this.room.height) - high / 2),
          width,
        };
        // And only to a frame that's really different, unless what must show would be cut.
        const change = target ? Math.max(Math.hypot(next.x - target.x, next.y - target.y), Math.abs(next.width - target.width)) / target.width : 1;
        if (change > 0.05 || (needed && !inside(needed, target, 0))) {
          target = next;
          follow.target = target;
        }
      }
    }
    if (!target) target = {...follow.at};
    // A new glide when the target moves: from here, at the speed it has now, to the target.
    const heading = follow.glide?.to || follow.at;
    if (keys.some(key => Math.abs(target[key] - heading[key]) > 0.5)) {
      const far = Math.hypot(target.x - follow.at.x, target.y - follow.at.y) / follow.at.width + Math.abs(Math.log(target.width / follow.at.width));
      const frames = Math.round(FPS * (0.7 + 0.5 * Math.min(1, far)));
      follow.glide = {from: {...follow.at}, speed: {...follow.speed}, to: {...target}, start: this.n, frames};
    }
    const glide = follow.glide;
    if (glide) {
      // A cubic Hermite: it leaves at the camera's own speed and arrives at rest.
      const t = Math.min(1, (this.n + 1 - glide.start) / glide.frames);
      const h00 = 2 * t ** 3 - 3 * t ** 2 + 1, h10 = t ** 3 - 2 * t ** 2 + t, h01 = -2 * t ** 3 + 3 * t ** 2;
      const before = {...follow.at};
      for (const key of keys) follow.at[key] = h00 * glide.from[key] + h10 * glide.speed[key] * glide.frames + h01 * glide.to[key];
      for (const key of keys) follow.speed[key] = follow.at[key] - before[key];
      if (t >= 1) {
        follow.glide = null;
        follow.at = {...glide.to};
        follow.speed = {x: 0, y: 0, width: 0};
      }
    }
    const w = follow.at.width;
    this.frame = {x: follow.at.x - w / 2, y: follow.at.y - (w * ratio) / 2, width: w, height: (w * tall) / out};
  }

  // One soft pass over what just changed: it washes in, holds and fades, while the film goes on.
  // `pad` (CSS px, or [across, down]) grows the mark around its target; a table cell's own
  // padding keeps it inside. `over` draws it above the app's panels.
  async highlight(target, {hold = 1.1, delay = 0, pad = -3, over = false} = {}) {
    const found = await this.rect(target);
    const [across, down] = Array.isArray(pad) ? pad : [pad, pad];
    const box = {x: found.x - across, y: found.y - down, width: found.width + across * 2, height: found.height + down * 2, over};
    const mark = {box, start: this.n + (delay ? seconds(delay) : 0), hold: seconds(hold)};
    this.highlights.push(mark);
  }

  // The film opens out of the band's ground, so a feed that loops comes back the way it left.
  async begin({fade = 0.35, from = 0.15} = {}) {
    let i = 0;
    const frames = seconds(fade);
    const out = bezier("settle");
    this.ending = from;
    await this.run(() => (this.ending = from * (1 - out(++i / frames))) <= 0);
  }

  // The film ends on a fade to the band's ground, picture, cursor and caption together, so a feed
  // that loops never snaps back to the opening.
  // It ends where it began, at the same depth of fade, so a feed's loop joins without a jump.
  async end({fade = 0.6, to = 0.15} = {}) {
    let i = 0;
    const frames = seconds(fade);
    await this.run(() => (this.ending = to * ease(++i / frames)) >= to - 1e-9);
  }

  // Keep the frame just shot: the poster, and the stills of the contact sheet.
  async mark(name) {
    if (this.dry) {
      this.marks[name] = {frame: this.n - 1, png: null};
      return;
    }
    if (!this.last) throw new Error(`Mark ${name} before any frame was shot.`);
    this.marks[name] = {frame: this.n - 1, png: this.last};
    // A following camera crops through the screen as it goes, as Screen Studio's does; a framed
    // shot shouldn't.
    const cut = this.follower ? [] : await this.app.evaluate(picture => window.__film.sliced(picture), this.onScreen(this.picture));
    if (cut.length) this.warn(`At ${name}, the crop cuts ${cut.slice(0, 3).map(text => `“${text}”`).join(", ")}.`);
    const sizes = (await this.app.evaluate(frame => window.__film.textSizes(frame), this.onScreen(this.picture)))
      .map(item => ({...item, size: Math.round((item.size * this.windowScale / this.scale) * 10) / 10}));
    const floor = SMALLEST_TEXT * Math.min(this.size.width, this.size.height);
    for (const item of sizes.filter(run => run.size < floor)) {
      const seen = this.small.get(item.text);
      this.small.set(item.text, {size: Math.min(item.size, seen?.size ?? Infinity), marks: new Set([...(seen?.marks || []), name])});
    }
  }

  // Whether a target is on screen and on top at its middle: not scrolled out of its panel or
  // covered by another layer.
  async shows(selector) {
    return this.app.evaluate(target => {
      const element = document.querySelector(target);
      const box = element?.getBoundingClientRect();
      if (!box?.width || !box.height) return false;
      const hit = document.elementsFromPoint(box.left + box.width / 2, box.top + box.height / 2).find(item => !item.closest("[data-film-overlay], [data-film-layer]"));
      return Boolean(hit && element.contains(hit));
    }, selector);
  }

  // ---- the machinery ----

  async rect(target, text = false) {
    if (target && typeof target === "object" && !Array.isArray(target) && "width" in target) return target;
    const box = await this.app.evaluate(([selectors, words]) => window.__film.rect(selectors, words), [target, text]);
    if (!box) throw new Error(`Nothing on screen matches ${JSON.stringify(target)}.`);
    return this.onStage(box);
  }

  // Run a track to its end, or start it and let later frames carry it.
  async run(track, wait = true) {
    if (!wait) {
      this.tracks.add(track);
      return;
    }
    let done = false;
    const mine = async () => (done = await track());
    this.tracks.add(mine);
    while (!done) await this.shoot();
  }

  within(point, target) {
    // A following camera brings the hand back into frame itself.
    if (this.follower) return;
    const picture = this.picture;
    if (point.x < picture.x || point.y < picture.y || point.x > picture.x + picture.width || point.y > picture.y + picture.height) {
      this.error(`The cursor reaches ${JSON.stringify(target)} outside the ${this.format} frame.`);
    }
  }

  warn(text) { if (!this.issues.warnings.includes(text)) this.issues.warnings.push(text); }
  error(text) { if (!this.issues.errors.includes(text)) this.issues.errors.push(text); }

  // One frame: the tracks move on, the pointer goes where the cursor is drawn (on a trip, onto the
  // caption band, which hides it from the page), time moves 1/30 s, every request lands, the
  // overlay is drawn and the frame is shot.
  // Time a stage of the shot.
  async timed(label, work) {
    const start = performance.now();
    try {
      return await work();
    } finally {
      this.timing[label] = (this.timing[label] ?? 0) + performance.now() - start;
    }
  }

  async shoot() {
    const start = performance.now();
    await this.timed("director", async () => {
      for (const track of [...this.tracks]) if (await track()) this.tracks.delete(track);
      if (this.follower) await this.track();
    });
    // On a trip the page sees the pointer only once the hand is over what it's going to, so a
    // hover starts as it enters, not when it stops.
    const {x, y} = this.cursor;
    const over = this.aim && x >= this.aim.x && x <= this.aim.x + this.aim.width && y >= this.aim.y && y <= this.aim.y + this.aim.height;
    if (this.cursor.visible) {
      const spot = this.travelling && !over ? await this.page.evaluate(() => window.__film.parking)
        : await this.page.evaluate(([frame, point]) => window.__film.toView(frame, point), [this.frame, {x, y}]);
      await this.page.mouse.move(spot.x, spot.y);
    }
    await this.tick();
    if (!this.recording) return;
    // Inside the screen: its marks, and which cursor it would show at the hand. Then the stage.
    const state = this.state();
    const point = state.cursor.visible ? this.onScreen({x: state.cursor.x, y: state.cursor.y}) : null;
    await this.timed("draw", async () => {
      state.cursor.kind = await this.app.evaluate(work => window.__film.frame(work),
        {highlights: state.highlights.map(mark => this.onScreen(mark)), cursor: point});
      await this.page.evaluate(state => window.__film.render(state), state);
    });
    if (this.dry) {
      // A frame played and drawn but not shot (a desk's scenes before `from`): every count, path
      // and transform moves on as in a full take, so what follows is filmed exactly as it would be.
      const shown = this.picture;
      this.lens.push([shown.x + shown.width / 2, shown.y + shown.height / 2, shown.width].map(value => Math.round(value * 10) / 10));
      this.trail.push(state.cursor.visible ? [(state.cursor.x - shown.x) / shown.width, (state.cursor.y - shown.y) / shown.height] : null);
      this.hashes.push(null);
      this.n++;
      return;
    }
    // The page is the shot, at the output's size.
    const png = await this.timed("screenshot", () => this.page.screenshot({type: "png"}));
    this.hashes.push(crypto.createHash("sha1").update(png).digest("hex").slice(0, 12));
    if (this.checking) {
      const cursor = state.cursor.visible ? await this.page.evaluate(([frame, point]) => window.__film.toView(frame, point), [this.frame, {x: state.cursor.x, y: state.cursor.y}]) : null;
      this.measured.push(await this.timed("probe", () => measureFrame(this.page, this.pages(), {f: this.n, cursor, products: this.products()})));
      this.followed[this.n] = Boolean(this.follower);
    }
    const shown = this.picture;
    // Where the camera looked, for the zig-zag check.
    this.lens.push([shown.x + shown.width / 2, shown.y + shown.height / 2, shown.width].map(value => Math.round(value * 10) / 10));
    // Where the hand was, in the picture's own terms (0 to 1 across and down), for the path check.
    this.trail.push(state.cursor.visible ? [(state.cursor.x - shown.x) / shown.width, (state.cursor.y - shown.y) / shown.height] : null);
    this.last = png;
    await this.timed("encode", () => this.sink(png));
    this.n++;
    this.timing.frames = (this.timing.frames ?? 0) + 1;
    this.timing.total = (this.timing.total ?? 0) + performance.now() - start;
  }

  // The pages the stage shows, by their iframe's id, for the probe.
  pages() { return {screen: this.app}; }
  // The pages that are the product, checked for the product's own motion.
  products() { return Object.keys(this.pages()); }

  // Check the frames measured since the last check, which start at frame `offset`.
  check(name, offset = 0) {
    if (!this.checking) return;
    const marks = Object.fromEntries(Object.entries(this.marks).map(([mark, {frame}]) => [mark, frame]));
    const accepts = [];
    for (let n = offset; n < offset + this.measured.length; n++) {
      if (!this.followed[n]) continue;
      const last = accepts.at(-1);
      if (last && last.frames[1] === n - 1) last.frames[1] = n;
      else accepts.push({rule: "layout/text-cut", frames: [n, n], reason: "the following camera crops through the screen as it goes, as Screen Studio's does"});
    }
    this.findings.push(...checkFrames(this.measured, {name, fps: FPS, offset, marks, accepts}));
    this.measured = [];
  }

  // Time moves 1/30 s in the window, and every request it made lands.
  async tick() {
    await this.timed("tick", async () => {
      await this.app.evaluate(() => window.__film.clock.step(1));
      await quiet(this.app);
    });
  }

  state() {
    const n = this.n;
    const current = this.captions.find(item => item.start <= n && (item.end === null || n < item.end));
    let caption = {text: "", opacity: 0, rise: 0};
    if (current) {
      const opening = Math.min(1, (n - current.start + 1) / 8);
      const closing = current.end === null ? 1 : Math.min(1, (current.end - n) / 5);
      caption = {text: current.text, opacity: Math.min(opening, closing), rise: 1 - ease(opening)};
    }
    // A click's ring is short, and shorter when the thing pressed goes.
    this.ripples = this.ripples.filter(ripple => n - ripple.start < RING && (ripple.gone === null || n - ripple.gone < 3));
    // The press: down to 0.9 in 80ms, back in 260ms, lifting like a finger.
    const {pressAt, releaseAt} = this.cursor;
    let press = 1;
    if (pressAt !== null && (releaseAt === null || releaseAt < pressAt)) press = 1 - 0.1 * PRESS_IN(Math.min(1, (n - pressAt) / (0.08 * FPS)));
    else if (releaseAt !== null) press = 0.9 + 0.1 * LET_GO(Math.min(1, (n - releaseAt) / (0.26 * FPS)));
    caption.opacity *= 1 - this.ending;
    const highlights = this.highlights.map(mark => {
      const age = n - mark.start;
      const rise = 8, fall = 15;
      const opacity = age < 0 ? 0 : age < rise ? age / rise : age < rise + mark.hold ? 1 : Math.max(0, 1 - (age - rise - mark.hold) / fall);
      return {...mark.box, opacity};
    }).filter(mark => mark.opacity > 0);
    return {
      frame: this.frame, size: this.size, band: this.size.band, caption, fade: Math.max(this.fade, this.ending), highlights,
      // The cursor goes with the picture through a cut or the ending, never floating on the dark.
      cursor: {x: this.cursor.x, y: this.cursor.y, visible: this.cursor.visible, press, opacity: 1 - Math.max(this.fade, this.ending)},
      ripples: this.ripples.map(ripple => ({x: ripple.x, y: ripple.y, progress: RING_CURVE((n - ripple.start) / RING), fading: ripple.gone === null ? 1 : 1 - (n - ripple.gone) / 3})),
    };
  }
}

// Wait until no request is in flight and the work it started has run, twice in a row.
export async function quiet(page) {
  for (let calm = 0, i = 0; calm < 2; i++) {
    if (i > 500) throw new Error("A request never finished.");
    const pending = await page.evaluate(async () => {
      await window.__film.clock.settle();
      return window.__film.pending;
    });
    calm = pending ? 0 : calm + 1;
    if (pending) await new Promise(resolve => setTimeout(resolve, 4));
  }
}

// A licensed font stylesheet (the hosted product's), fetched here and served from the film's own
// origin, so nothing proprietary lands in the repository and the page makes no outside request.
export async function loadFonts(stylesheet) {
  if (!stylesheet) return null;
  const get = async url => {
    const response = await fetch(url, {signal: AbortSignal.timeout(20000)});
    if (!response.ok) throw new Error(`Couldn't fetch ${url}: ${response.status}.`);
    return response;
  };
  const css = await (await get(stylesheet)).text();
  const files = new Map();
  const local = await Promise.all([...css.matchAll(/url\("?([^")]+)"?\)/g)].map(async ([, url]) => {
    const name = path.basename(new URL(url, stylesheet).pathname);
    files.set(name, Buffer.from(await (await get(new URL(url, stylesheet))).arrayBuffer()));
    return [url, `/film-fonts/${name}`];
  }));
  let rewritten = css;
  for (const [from, to] of local) rewritten = rewritten.replaceAll(from, to);
  files.set("fonts.css", Buffer.from(rewritten));
  return files;
}

export function ffmpeg(args) {
  const child = spawn("ffmpeg", ["-loglevel", "error", "-y", ...args], {stdio: ["pipe", "ignore", "pipe"]});
  let stderr = "";
  child.stderr.on("data", chunk => (stderr += chunk));
  // A missing or failed ffmpeg is said by `done`; the pipe it leaves broken is not a second error.
  child.stdin.on("error", () => {});
  const done = new Promise((resolve, reject) => {
    child.on("error", error => reject(new Error(`ffmpeg: ${error.message}`)));
    child.on("close", code => (code ? reject(new Error(`ffmpeg: ${stderr.trim()}`)) : resolve()));
  });
  // Whoever awaits `done` still gets its failure; until then it is not an unhandled rejection.
  done.catch(() => {});
  return {child, done};
}

export async function encodeStills(pngs, args) {
  const {child, done} = ffmpeg(["-f", "image2pipe", "-c:v", "png", "-i", "-", ...args]);
  for (const png of pngs) child.stdin.write(png);
  child.stdin.end();
  await done;
}

// The same captions as WebVTT, which a browser's video plays as a track.
export function vtt(captions, total) {
  const time = frame => {
    const ms = Math.round((frame / FPS) * 1000);
    const pad = (value, size = 2) => String(value).padStart(size, "0");
    return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)}.${pad(ms % 1000, 3)}`;
  };
  return `WEBVTT\n\n${captions.map(item => `${time(item.start)} --> ${time(Math.min(item.end ?? total, total))}\n${item.text}\n`).join("\n")}`;
}

export function srt(captions, total) {
  const time = frame => {
    const ms = Math.round((frame / FPS) * 1000);
    const pad = (value, size = 2) => String(value).padStart(size, "0");
    return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)},${pad(ms % 1000, 3)}`;
  };
  return captions.map((item, index) => `${index + 1}\n${time(item.start)} --> ${time(Math.min(item.end ?? total, total))}\n${item.text}\n`).join("\n");
}

// The stage: the backdrop, and the dashboard in its own window, an iframe exactly the screen's
// size, `margin` in from the corner. A corner is rounded where both its sides sit on the
// backdrop; an edge that runs off the picture stays square. The hairline edge, and on white a
// short shadow that fades out above the band. The app's stylesheet gives the captions its type.
const STAGE = "/__film/stage";
function stagePage({screen, margin: m, room, view, backdrop, theme, fonts, start}) {
  const round = (a, b) => (a && b ? "12px" : "0");
  const edge = theme === "light" ? "0 0 0 1px rgb(0 0 0 / 9%), 0 2px 10px rgb(0 0 0 / 7%)" : "0 0 0 1px rgb(255 255 255 / 9%)";
  const framed = m.top || m.right || m.bottom || m.left;
  return `<!doctype html><html data-theme="${theme}"><head><meta charset="utf-8">
<link rel="stylesheet" href="/assets/app.css">${fonts ? '<link rel="stylesheet" href="/film-fonts/fonts.css">' : ""}
<style>
  html, body { margin: 0 !important; padding: 0 !important; width: ${view.width}px; height: ${view.height}px; min-height: 0 !important; overflow: hidden !important; background: ${backdrop} !important; }
  #room { position: absolute; left: 0; top: 0; width: ${room.width}px; height: ${room.height}px; transform-origin: 0 0; }
  #screen { position: absolute; left: ${m.left}px; top: ${m.top}px; width: ${screen.width}px; height: ${screen.height}px; border: 0; display: block; background: ${backdrop};
    ${framed ? `border-radius: ${round(m.top, m.left)} ${round(m.top, m.right)} ${round(m.bottom, m.right)} ${round(m.bottom, m.left)}; box-shadow: ${edge};` : ""} }
</style></head><body><div id="room"><iframe id="screen" src="${start}"></iframe></div></body></html>`;
}

// Where a take goes inside its film's folder: one per format and theme.
export const takeName = (format, theme = "dark") => `${format.replace(":", "x")}${theme === "dark" ? "" : `-${theme}`}`;

// Film one format: serve the world, open the dashboard under the virtual clock, play the script.
// A rehearsal (`rehearse`) shoots the same layout at one device px per CSS px instead of two, a
// quarter of the pixels, encodes fast and skips the GIF and the motion strips: for working out a
// film, with every check on. The final take is shot at two.
// A film's world is Tin's dashboard on a demo world (`serve`), or a live site played from its
// recording (`site`: {url, recording, reads, answer, secrets, session}, recording.js). `record`
// plays the site live where its recording has no answer yet, and saves what it gets. `session` is
// a signed-in browser's storage state (Playwright's), for a site behind sign-in: its cookies go
// with each request and are never saved, and their values are among the secrets looked for.
export async function shoot(film, {format = "4:5", theme = film.theme || "dark", out, fonts = null, encode = true, check = false, rehearse = false, record = false, gif = !rehearse} = {}) {
  const size = FORMATS[format];
  if (!size) throw new Error(`Unknown format ${format}; use ${Object.keys(FORMATS).join(", ")}.`);
  const world = film.world();
  const now = Date.parse(film.now);
  const served = await serveApp({decisionCount: 0, ...world.serve, now: () => new Date(now)});
  const site = world.site;
  const recordingDir = site && path.resolve(".films/worlds", site.recording);
  const recording = site && openRecording(recordingDir, {record, reads: site.reads, answer: site.answer});
  const {screen, margin, room, viewport} = stage(size, film.screens?.[format]);
  const browser = await chromium.launch({headless: true});
  const issues = {errors: [], warnings: [], blocked: []};
  const dir = path.join(out, film.name, takeName(format, theme));
  fs.rmSync(dir, {recursive: true, force: true});
  fs.mkdirSync(dir, {recursive: true});
  const mp4 = path.join(dir, "film.mp4");
  const encoder = encode && ffmpeg(["-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
    "-vf", `scale=${size.width}:${size.height}:flags=lanczos,format=yuv420p`, "-c:v", "libx264", ...(rehearse ? ["-preset", "ultrafast", "-crf", "23"] : ["-preset", "slow", "-crf", "18"]),
    "-movflags", "+faststart", "-an", mp4]);
  try {
    // A number, or one per format: a tall film that closes in on a panel needs more pixels.
    // The stage page is the shot: the output's size at two device px per CSS px. The camera moves
    // the room inside it, so text is drawn at whatever scale the camera needs.
    const deviceScale = 2;
    const view = {width: size.width / deviceScale, height: size.height / deviceScale};
    const context = await browser.newContext({viewport: view, deviceScaleFactor: rehearse ? 1 : deviceScale, colorScheme: theme, timezoneId: film.timezone || "UTC", locale: "en-US", ...(site?.session ? {storageState: site.session} : {})});
    const backdrop = BACKDROP[theme] || BACKDROP.dark;
    await context.route("**/*", async route => {
      const url = route.request().url();
      if (!url.startsWith(served.base)) {
        if (recording) return recording.route(route);
        issues.blocked.push(new URL(url).origin);
        return route.abort();
      }
      const {pathname} = new URL(url);
      if (pathname === STAGE) return route.fulfill({body: stagePage({screen, margin, room, view, backdrop, theme, fonts: Boolean(fonts), start: site ? site.url : `${world.start || "/system"}?project=project`}), contentType: "text/html"});
      // Clerk is stubbed (below); the fixture leaves its script address unfilled.
      if (decodeURIComponent(pathname).startsWith("/{{CLERK_FRONTEND_API_URL}}/")) return route.fulfill({body: "", contentType: "text/javascript"});
      if (pathname.startsWith("/film-fonts/") && fonts?.has(path.basename(pathname))) {
        return route.fulfill({body: fonts.get(path.basename(pathname)), contentType: pathname.endsWith(".css") ? "text/css" : "font/woff2"});
      }
      // As the hosted product does: the licensed stylesheet goes where index.html asks for it.
      if (fonts && route.request().resourceType() === "document") {
        const response = await route.fetch();
        const html = (await response.text()).replace("<!--PRIVATE_FONTS_STYLESHEET-->", '<link rel="stylesheet" href="/film-fonts/fonts.css" />');
        return route.fulfill({response, body: html});
      }
      return route.continue();
    });
    await context.addInitScript(`(${installVirtualClock})(${JSON.stringify({fps: FPS, epoch: now})});`);
    await context.addInitScript(`(${installScreen})();`);
    await context.addInitScript(`(${installStage})(${JSON.stringify({backdrop, path: STAGE, size, dpr: deviceScale})});`);
    await context.addInitScript(({member, theme, image}) => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: member, imageUrl: image}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
      localStorage.setItem("tin-lite:theme", theme);
    }, {member: world.member || "Avery", theme, image: avatar(world.member || "Avery")});
    const page = await context.newPage();
    // Tin's own dashboard must film without an error; a live site's own errors are its own, so
    // they are said and the take goes on.
    const faults = site ? issues.warnings : issues.errors;
    const fault = text => faults.includes(text) || faults.push(text);
    page.on("pageerror", error => fault(`The page threw: ${error.message}`));
    page.on("console", message => {
      if (message.type() !== "error") return;
      const where = message.location()?.url;
      fault(`The page logged an error: ${message.text()}${where ? ` (${where.replace(served.base, "")})` : ""}`);
    });
    await page.goto(`${served.base}${STAGE}`, {waitUntil: "domcontentloaded"});
    const app = await (await page.waitForSelector("#screen")).contentFrame();
    await app.waitForLoadState("domcontentloaded");
    await page.evaluate(() => document.fonts.ready);

    const director = new Director({page, app, format, size, screen, margin, room, deviceScale, served, issues, ripple: Boolean(film.ripple), check});
    director.sink = async png => {
      if (!encoder) return;
      // An encoder that has stopped never drains: its end, or its failure, stops the take.
      if (!encoder.child.stdin.write(png)) {
        await Promise.race([new Promise(resolve => encoder.child.stdin.once("drain", resolve)),
          encoder.done.then(() => { throw new Error("ffmpeg stopped before the film ended"); })]);
      }
    };
    // The dashboard boots without being filmed.
    director.recording = false;
    for (let i = 0; i < 20 * FPS && !(await app.locator(world.ready || "main").count()); i++) await director.shoot();
    // What a film does before it starts, unfilmed: a site's cookie banner dismissed, a popup
    // closed. The hand that did it is not in the film.
    if (film.prepare) {
      await player({beats: film.prepare}, film.values, "prepare")(director);
      Object.assign(director, {journeys: [], events: [], moves: []});
      director.cursor.visible = false;
    }
    // A second more, unfilmed, so what the page draws once on arrival has settled, as a desk does.
    for (let i = 0; i < FPS; i++) await director.shoot();
    await app.evaluate(() => document.fonts.ready);
    await page.evaluate(() => document.fonts.ready);
    director.recording = true;
    await film.play(director);
    director.caption(null);
    director.check(film.name);
    director.judgeCamera();
    director.judgeHand();
    director.judgeLens();
    director.judgeText();
    await context.close();

    issues.blocked = [...new Set(issues.blocked)];
    if (!director.marks.poster) issues.errors.push("The film never marks its poster.");
    if (director.n / FPS > 140) issues.warnings.push("Longer than X's 2 minutes 20 seconds.");
    const recorded = recording?.close();
    if (recorded?.misses.length) issues.errors.push(`The recording ${site.recording} has no answer for ${recorded.misses.length} request${recorded.misses.length === 1 ? "" : "s"}${record ? " (the live site didn't answer)" : "; record it again with --record on"}: ${recorded.misses.slice(0, 5).join(", ")}.`);
    if (recorded?.stopped.length) issues.errors.push(`The film reached for ${recorded.stopped.length} outside effect${recorded.stopped.length === 1 ? "" : "s"} live, which a recording never sends; stage the answer, or name it in reads if it only reads: ${recorded.stopped.slice(0, 5).join(", ")}.`);
    const cookies = site?.session ? JSON.parse(fs.readFileSync(site.session, "utf8")).cookies?.map(cookie => cookie.value) ?? [] : [];
    const leaks = site ? findSecrets(recordingDir, [...(site.secrets || []), ...cookies]) : [];
    if (leaks.length) issues.errors.push(`The recording ${site.recording} holds a secret in ${leaks.length} answer${leaks.length === 1 ? "" : "s"}: ${leaks.slice(0, 3).join(", ")}. Delete those answers and record again.`);
    const report = {film: film.name, title: film.title, format, size, frames: director.n, seconds: director.n / FPS, captions: director.captions.map(item => ({text: item.text, start: item.start / FPS, end: Math.min(item.end ?? director.n, director.n) / FPS})), hashes: director.hashes, trail: director.trail, lens: director.lens, issues};
    if (recorded) report.recording = {name: site.recording, ...recorded};
    if (director.checking) report.checks = director.findings.map(describe);
    if (director.checking) report.summary = summary(director.findings);
    if (encoder) {
      encoder.child.stdin.end();
      await encoder.done;
      await stills(director, size, dir, {gif, light: rehearse});
      if (!rehearse) await motionStrips(director, size, dir);
      fs.writeFileSync(path.join(dir, "captions.srt"), srt(director.captions, director.n));
      fs.writeFileSync(path.join(dir, "captions.vtt"), vtt(director.captions, director.n));
      report.bytes = Object.fromEntries(fs.readdirSync(dir).filter(name => name.includes(".")).map(name => [name, fs.statSync(path.join(dir, name)).size]));
      if (report.bytes["film.gif"] > 15 * 1024 * 1024) issues.warnings.push("The GIF is over X's 15 MB.");
    }
    fs.writeFileSync(path.join(dir, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
    return {dir, report};
  } catch (error) {
    // A film that stops part way leaves no half-written video behind. ffmpeg blocked on its input
    // won't act on a polite signal, so its input is closed and it is killed outright.
    if (encoder && encoder.child.exitCode === null) {
      encoder.child.stdin.destroy();
      encoder.child.kill("SIGKILL");
      await encoder.done.catch(() => {});
    }
    fs.rmSync(mp4, {force: true});
    throw error;
  } finally {
    await browser.close();
    served.server.close();
  }
}

// Each mark at full size (the poster among them), a contact sheet of them, and a GIF small
// enough for X (15 MB). A rehearsal's stills (`light`) are JPEGs at the half size it was shot
// at: whoever looks at them, an agent with a bounded context among them, reads a tenth of the
// bytes.
export async function stills(director, size, dir, {gif = true, light = false} = {}) {
  fs.mkdirSync(path.join(dir, "marks"));
  const kind = light ? "jpg" : "png";
  const scale = light ? 0.5 : 1;
  const quality = light ? ["-q:v", "3"] : [];
  // A mark in a scene played dry has no frame to show.
  const marks = Object.entries(director.marks).filter(([, mark]) => mark.png).sort(([, a], [, b]) => a.frame - b.frame);
  const files = [];
  for (const [index, [name, mark]] of marks.entries()) {
    const file = path.join(dir, "marks", `${String(index + 1).padStart(2, "0")}-${name}.${kind}`);
    await encodeStills([mark.png], ["-vf", `scale=${size.width * scale}:${size.height * scale}:flags=lanczos`, ...quality, "-frames:v", "1", file]);
    files.push(file);
    if (name === "poster") fs.copyFileSync(file, path.join(dir, `poster.${kind}`));
  }
  // Every still is the same size by now: ffmpeg's tile starts over whenever the size changes.
  const columns = Math.min(3, files.length);
  await encodeStills(marks.map(([, mark]) => mark.png), ["-vf", `scale=${Math.round(size.width / 2.5)}:-2:flags=lanczos,tile=${columns}x${Math.ceil(files.length / columns)}:padding=24:margin=24:color=0x44403a`, ...quality, "-frames:v", "1", path.join(dir, `sheet.${kind}`)]);
  if (!gif) return;
  const width = Math.round(size.width * (size.width > size.height ? 0.5 : 0.6) / 2) * 2;
  await ffmpeg(["-i", path.join(dir, "film.mp4"), "-vf", `fps=15,scale=${width}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4`, path.join(dir, "film.gif")]).done;
}

// Every camera move, click and cut as a strip of every other frame, for looking at the motion
// itself (Stageshow's rehearse: film each beat at 40 to 80ms and look). Stills never show it.
export async function motionStrips(director, size, dir) {
  fs.mkdirSync(path.join(dir, "motion"));
  for (const [index, event] of director.events.entries()) {
    const first = Math.max(0, event.start);
    const last = Math.min(director.n - 1, event.end);
    const count = Math.floor((last - first) / 2) + 1;
    if (count < 2) continue;
    const columns = Math.min(8, count);
    const name = `${String(index + 1).padStart(2, "0")}-${event.label.replace(/\W+/g, "-")}-${(first / FPS).toFixed(1)}s.png`;
    await ffmpeg(["-i", path.join(dir, "film.mp4"), "-vf", `select='between(n\\,${first}\\,${last})*not(mod(n-${first}\\,2))',scale=${Math.round(size.width / 4)}:-2,tile=${columns}x${Math.ceil(count / columns)}:padding=6:color=0x44403a`,
      "-frames:v", "1", "-fps_mode", "vfr", path.join(dir, "motion", name)]).done;
  }
}

// A film written as JSON, as the feature-film workflow's agent writes one: a live site, its
// beats and the named film's facts. Its recording and takes sit beside the file.
export function filmFromSpec(spec, file) {
  const problems = [];
  if (!spec || typeof spec !== "object" || Array.isArray(spec)) throw new Error(`${file} is not a film: it must hold one JSON object.`);
  if (typeof spec.title !== "string" || !spec.title.trim()) problems.push("a title");
  if (!/^https:\/\/[^\s/]+/.test(spec.site?.url ?? "")) problems.push("site.url, the https address the film opens on");
  if (Number.isNaN(Date.parse(spec.now))) problems.push("now, the moment the page's clock shows, as an ISO time");
  if (!Array.isArray(spec.beats) || !spec.beats.length) problems.push("beats");
  if (problems.length) throw new Error(`${file} needs ${problems.join(", ")}.`);
  const here = path.dirname(path.resolve(file));
  const site = {...spec.site, recording: path.join(here, "world")};
  return {name: spec.name || "film", ...spec, world: () => ({site, ready: spec.ready || "body"}), play: player(spec)};
}

export async function loadFilm(name) {
  if (name.endsWith(".json")) return filmFromSpec(JSON.parse(fs.readFileSync(name, "utf8")), name);
  const file = path.resolve("web/film/films", `${name}.js`);
  if (!fs.existsSync(file)) throw new Error(`No film named ${name} in web/film/films.`);
  const film = await import(pathToFileURL(file));
  // A film is code (`play`) or data (`beats`, with its named `values`); either way it plays here.
  return {name, ...film, play: player(film, film.values)};
}

async function main(argv) {
  const options = {format: "4:5,16:9", theme: "dark", out: ".films", fonts: process.env.TIN_FILM_FONTS || null, check: "off", rehearse: "off", record: "off"};
  const names = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) options[argv[i].slice(2)] = argv[++i];
    else names.push(argv[i]);
  }
  if (!names.length) throw new Error("Name a film, for example: npm run film -- people-requests");
  const fonts = await loadFonts(options.fonts);
  let failed = false;
  for (const name of names) {
    const film = await loadFilm(name);
    for (const format of options.format.split(",")) {
      // A rehearsal is for working a film out, so its checks are always on.
      const rehearse = options.rehearse === "on";
      const {dir, report} = await shoot(film, {format, theme: options.theme, out: options.out, fonts, check: rehearse || options.check === "on", rehearse, record: options.record === "on"});
      const {errors, warnings, blocked} = report.issues;
      console.log(`${film.name} ${format}: ${report.frames} frames, ${report.seconds.toFixed(1)}s → ${dir}`);
      for (const line of errors) console.log(`  error: ${line}`);
      for (const line of warnings) console.log(`  warning: ${line}`);
      if (blocked.length) console.log(`  blocked: ${blocked.join(", ")}`);
      for (const line of report.summary ?? []) console.log(`  check: ${line}`);
      failed ||= errors.length > 0;
    }
  }
  if (failed) process.exitCode = 1;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
