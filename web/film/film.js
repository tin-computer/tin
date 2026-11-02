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
import {serveApp} from "../app-fixture.js";
import {installVirtualClock} from "./clock.js";
import {bezier} from "./ease.js";
import {installOverlay} from "./overlay.js";

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
// The smallest text a frame may show, as a share of its short side: about 22px at 1080.
const SMALLEST_TEXT = 0.02;
// The cursor's travel, as Stageshow's act.move eases it; everything else uses the named curves in
// ease.js.
const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const lerp = (a, b, t) => a + (b - a) * t;
const seconds = s => Math.max(1, Math.round(s * FPS));
// Stageshow's cursor timing (kit/cursor.css): the press, its release, and the ring a click can
// send out, 0.55 s on its own curve.
const PRESS_IN = bezier([0, 0, 0.58, 1]);
const LET_GO = bezier([0.22, 1, 0.36, 1]);
const RING = Math.round(0.55 * FPS);
const RING_CURVE = bezier([0.25, 0.8, 0.25, 1]);

class Director {
  constructor({page, format, size, screen, margin = {top: 0, right: 0, bottom: 0, left: 0}, room = screen, deviceScale, served, issues, ripple = false}) {
    Object.assign(this, {page, format, size, screen, margin, room, deviceScale, world: served, issues, ripple});
    this.wide = size.width > size.height;
    this.n = 0;
    this.recording = true;
    this.tracks = new Set();
    this.captions = [];
    this.ripples = [];
    this.marks = {};
    this.hashes = [];
    this.cursor = {x: 0, y: 0, visible: false, pressAt: null, releaseAt: null};
    // What moved when, for the camera rules and the motion strips.
    this.moves = [];
    this.events = [];
    this.cameraMoving = false;
    this.fade = 0;
    this.ending = 0;
    this.highlights = [];
    this.travelling = false;
    // Text under the smallest size, by what it says: the least size and the marks it shows at.
    this.small = new Map();
    this.inside = null;
    this.frame = {x: 0, y: 0, width: screen.width, height: screen.width * size.height / size.width};
    this.last = null;
  }

  // CSS px of page per output px, and the part of the frame above the caption band.
  get scale() { return this.frame.width / this.size.width; }
  get picture() { return {...this.frame, height: (this.size.height - this.size.band) * this.scale}; }

  // ---- what a film script calls ----

  async open(route, {ready} = {}) {
    await this.page.evaluate(to => {
      history.pushState(null, "", to);
      dispatchEvent(new PopStateEvent("popstate"));
    }, `${route}${route.includes("?") ? "&" : "?"}project=project`);
    if (ready) await this.ready(ready);
  }

  // Frames go by (recorded) until something is visible, or until it has gone.
  async ready(selector, {within = 10} = {}) {
    for (let i = 0; i < within * FPS; i++) {
      if (await this.page.locator(selector).first().isVisible()) return;
      await this.shoot();
    }
    throw new Error(`Nothing matched ${selector} within ${within}s.`);
  }

  async gone(selector, {within = 10} = {}) {
    for (let i = 0; i < within * FPS; i++) {
      if (!(await this.page.locator(selector).count())) return;
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
    const region = inside && (await this.page.evaluate(selector => window.__film.rect(selector), inside));
    if (region && frame.width <= region.width) frame.x = Math.min(Math.max(region.x, frame.x), region.x + region.width - frame.width);
    const top = valign === "top" ? box.y - pad : box.y + box.height / 2 - (room * scale) / 2;
    frame.y = Math.max(0, Math.min(top, this.room.height - room * scale));
    if (nudge) {
      const shift = await this.page.evaluate(([frame, picture, room, nudge, top]) => window.__film.clean(frame, picture, room, nudge, top),
        [frame, room * scale, this.room, nudge, floor]);
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
    await this.run(() => {
      i++;
      const t = ease(i / frames);
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
    this.events.push({label: "click", start: this.n - 4, end: this.n + 14});
    await this.hold(0.12);
    const before = await this.page.evaluate(selectors => window.__film.rect(selectors), target);
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
      const there = await this.page.evaluate(([selectors, x, y]) => window.__film.hits(selectors, x, y), [target, ripple.x, ripple.y]);
      if (!there) ripple.gone = this.n;
      return !there;
    });
    // The page shouldn't move what was just clicked out from under the cursor. Checked a few
    // frames on, as the next calls go.
    let frames = 0;
    this.tracks.add(async () => {
      if (++frames < 6) return false;
      const after = before && (await this.page.evaluate(selectors => window.__film.rect(selectors), target));
      const moved = after ? Math.round(Math.hypot(after.x - before.x, after.y - before.y)) : 0;
      if (moved > 4) this.warn(`Clicking ${JSON.stringify(target)} moved it ${moved}px: the page shifted under the cursor.`);
      return true;
    });
  }

  // Scroll a container until a target inside it sits `offset` CSS px below its top edge.
  async scroll(container, target, {duration = 0.8, offset = 24} = {}) {
    const [from, to] = await this.page.evaluate(([outer, inner, gap]) => {
      const box = document.querySelector(outer);
      const top = box.scrollTop + document.querySelector(inner).getBoundingClientRect().top - box.getBoundingClientRect().top - gap;
      return [box.scrollTop, Math.max(0, Math.min(top, box.scrollHeight - box.clientHeight))];
    }, [container, target, offset]);
    const frames = seconds(duration);
    let i = 0;
    await this.run(async () => {
      i++;
      await this.page.evaluate(([outer, top]) => (document.querySelector(outer).scrollTop = top), [container, lerp(from, to, ease(i / frames))]);
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

  // Text too small to read on a phone, said once for the film, smallest first.
  judgeText() {
    if (!this.small.size) return;
    const floor = Math.round(SMALLEST_TEXT * Math.min(this.size.width, this.size.height));
    const runs = [...this.small].sort((a, b) => a[1].size - b[1].size);
    const marks = [...new Set(runs.flatMap(([, item]) => [...item.marks]))];
    this.warn(`${runs.length} text runs are under ${floor}px (at ${marks.join(", ")}): ${runs.slice(0, 5).map(([text, item]) => `“${text}” ${item.size}px`).join(", ")}.`);
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
    if (!this.last) throw new Error(`Mark ${name} before any frame was shot.`);
    this.marks[name] = {frame: this.n - 1, png: this.last};
    const cut = await this.page.evaluate(picture => window.__film.sliced(picture), this.picture);
    if (cut.length) this.warn(`At ${name}, the crop cuts ${cut.slice(0, 3).map(text => `“${text}”`).join(", ")}.`);
    const sizes = await this.page.evaluate(([frame, scale]) => window.__film.textSizes(frame, scale), [this.picture, 1 / this.scale]);
    const floor = SMALLEST_TEXT * Math.min(this.size.width, this.size.height);
    for (const item of sizes.filter(run => run.size < floor)) {
      const seen = this.small.get(item.text);
      this.small.set(item.text, {size: Math.min(item.size, seen?.size ?? Infinity), marks: new Set([...(seen?.marks || []), name])});
    }
  }

  // Whether a target is on screen and on top at its middle: not scrolled out of its panel or
  // covered by another layer.
  async shows(selector) {
    return this.page.evaluate(target => {
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
    const box = await this.page.evaluate(([selectors, words]) => window.__film.rect(selectors, words), [target, text]);
    if (!box) throw new Error(`Nothing on screen matches ${JSON.stringify(target)}.`);
    return box;
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
  async shoot() {
    for (const track of [...this.tracks]) if (await track()) this.tracks.delete(track);
    // On a trip the page sees the pointer only once the hand is over what it's going to, so a
    // hover starts as it enters, not when it stops.
    const band = {x: this.frame.x + this.frame.width / 2, y: this.frame.y + (this.size.height - this.size.band / 2) * this.scale};
    const {x, y} = this.cursor;
    const over = this.aim && x >= this.aim.x && x <= this.aim.x + this.aim.width && y >= this.aim.y && y <= this.aim.y + this.aim.height;
    if (this.cursor.visible) await this.page.mouse.move(...(this.travelling && !over ? [band.x, band.y] : [x, y]));
    await this.page.evaluate(() => window.__film.clock.step(1));
    await quiet(this.page);
    if (!this.recording) return;
    await this.page.evaluate(state => window.__film.render(state), this.state());
    const clip = Object.fromEntries(Object.entries(this.frame).map(([key, value]) => [key, Math.round(value * 2) / 2]));
    const png = await this.page.screenshot({clip, type: "png"});
    this.hashes.push(crypto.createHash("sha1").update(png).digest("hex").slice(0, 12));
    this.last = png;
    await this.sink(png);
    this.n++;
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
async function quiet(page) {
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
async function loadFonts(stylesheet) {
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

function ffmpeg(args) {
  const child = spawn("ffmpeg", ["-loglevel", "error", "-y", ...args], {stdio: ["pipe", "ignore", "pipe"]});
  let stderr = "";
  child.stderr.on("data", chunk => (stderr += chunk));
  const done = new Promise((resolve, reject) => child.on("close", code => (code ? reject(new Error(`ffmpeg: ${stderr.trim()}`)) : resolve())));
  return {child, done};
}

async function encodeStills(pngs, args) {
  const {child, done} = ffmpeg(["-f", "image2pipe", "-c:v", "png", "-i", "-", ...args]);
  for (const png of pngs) child.stdin.write(png);
  child.stdin.end();
  await done;
}

function srt(captions, total) {
  const time = frame => {
    const ms = Math.round((frame / FPS) * 1000);
    const pad = (value, size = 2) => String(value).padStart(size, "0");
    return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)},${pad(ms % 1000, 3)}`;
  };
  return captions.map((item, index) => `${index + 1}\n${time(item.start)} --> ${time(Math.min(item.end ?? total, total))}\n${item.text}\n`).join("\n");
}

// Film one format: serve the world, open the dashboard under the virtual clock, play the script.
export async function shoot(film, {format = "4:5", theme = film.theme || "dark", out, fonts = null, encode = true} = {}) {
  const size = FORMATS[format];
  if (!size) throw new Error(`Unknown format ${format}; use ${Object.keys(FORMATS).join(", ")}.`);
  const world = film.world();
  const now = Date.parse(film.now);
  const served = await serveApp({decisionCount: 0, ...world.serve, now: () => new Date(now)});
  const {screen, margin, room, viewport} = stage(size, film.screens?.[format]);
  const browser = await chromium.launch({headless: true});
  const issues = {errors: [], warnings: [], blocked: []};
  const dir = path.join(out, film.name, `${format.replace(":", "x")}${theme === "dark" ? "" : `-${theme}`}`);
  fs.rmSync(dir, {recursive: true, force: true});
  fs.mkdirSync(dir, {recursive: true});
  const mp4 = path.join(dir, "film.mp4");
  const encoder = encode && ffmpeg(["-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
    "-vf", `scale=${size.width}:${size.height}:flags=lanczos,format=yuv420p`, "-c:v", "libx264", "-preset", "slow", "-crf", "18",
    "-movflags", "+faststart", "-an", mp4]);
  try {
    // A number, or one per format: a tall film that closes in on a panel needs more pixels.
    const deviceScale = (typeof film.deviceScale === "object" ? film.deviceScale[format] : film.deviceScale) || 2;
    const context = await browser.newContext({viewport, deviceScaleFactor: deviceScale, colorScheme: theme, timezoneId: film.timezone || "UTC", locale: "en-US"});
    await context.route("**/*", async route => {
      const url = route.request().url();
      if (!url.startsWith(served.base)) {
        issues.blocked.push(new URL(url).origin);
        return route.abort();
      }
      const {pathname} = new URL(url);
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
    await context.addInitScript(`(${installOverlay})(${JSON.stringify({screen, margin, backdrop: BACKDROP[theme] || BACKDROP.dark, theme})});`);
    await context.addInitScript(({member, theme}) => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: member}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
      localStorage.setItem("tin-lite:theme", theme);
    }, {member: world.member || "Avery", theme});
    const page = await context.newPage();
    page.on("pageerror", error => issues.errors.push(`The page threw: ${error.message}`));
    page.on("console", message => {
      if (message.type() !== "error") return;
      const where = message.location()?.url;
      issues.errors.push(`The page logged an error: ${message.text()}${where ? ` (${where.replace(served.base, "")})` : ""}`);
    });
    await page.goto(`${served.base}${world.start || "/system"}?project=project`, {waitUntil: "domcontentloaded"});
    await page.evaluate(() => document.fonts.ready);

    const director = new Director({page, format, size, screen, margin, room, deviceScale, served, issues, ripple: Boolean(film.ripple)});
    director.sink = async png => {
      if (!encoder) return;
      if (!encoder.child.stdin.write(png)) await new Promise(resolve => encoder.child.stdin.once("drain", resolve));
    };
    // The dashboard boots without being filmed.
    director.recording = false;
    for (let i = 0; i < 20 * FPS && !(await page.locator(world.ready || "main").count()); i++) await director.shoot();
    await page.evaluate(() => document.fonts.ready);
    director.recording = true;
    await film.play(director);
    director.caption(null);
    director.judgeCamera();
    director.judgeText();
    await context.close();

    issues.blocked = [...new Set(issues.blocked)];
    if (!director.marks.poster) issues.errors.push("The film never marks its poster.");
    if (director.n / FPS > 140) issues.warnings.push("Longer than X's 2 minutes 20 seconds.");
    const report = {film: film.name, title: film.title, format, size, frames: director.n, seconds: director.n / FPS, hashes: director.hashes, issues};
    if (encoder) {
      encoder.child.stdin.end();
      await encoder.done;
      await stills(director, size, dir);
      await motionStrips(director, size, dir);
      fs.writeFileSync(path.join(dir, "captions.srt"), srt(director.captions, director.n));
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
// enough for X (15 MB).
async function stills(director, size, dir) {
  const scale = `scale=${size.width}:${size.height}:flags=lanczos`;
  fs.mkdirSync(path.join(dir, "marks"));
  const marks = Object.entries(director.marks).sort(([, a], [, b]) => a.frame - b.frame);
  const files = [];
  for (const [index, [name, mark]] of marks.entries()) {
    const file = path.join(dir, "marks", `${String(index + 1).padStart(2, "0")}-${name}.png`);
    await encodeStills([mark.png], ["-vf", scale, "-frames:v", "1", file]);
    files.push(file);
    if (name === "poster") fs.copyFileSync(file, path.join(dir, "poster.png"));
  }
  // Every still is the same size by now: ffmpeg's tile starts over whenever the size changes.
  const columns = Math.min(3, files.length);
  await encodeStills(files.map(file => fs.readFileSync(file)), ["-vf", `scale=${Math.round(size.width / 2.5)}:-2:flags=lanczos,tile=${columns}x${Math.ceil(files.length / columns)}:padding=24:margin=24:color=0x44403a`, "-frames:v", "1", path.join(dir, "sheet.png")]);
  const width = Math.round(size.width * (size.width > size.height ? 0.5 : 0.6) / 2) * 2;
  await ffmpeg(["-i", path.join(dir, "film.mp4"), "-vf", `fps=15,scale=${width}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4`, path.join(dir, "film.gif")]).done;
}

// Every camera move, click and cut as a strip of every other frame, for looking at the motion
// itself (Stageshow's rehearse: film each beat at 40 to 80ms and look). Stills never show it.
async function motionStrips(director, size, dir) {
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

export async function loadFilm(name) {
  const file = path.resolve("web/film/films", `${name}.js`);
  if (!fs.existsSync(file)) throw new Error(`No film named ${name} in web/film/films.`);
  return {name, ...(await import(pathToFileURL(file)))};
}

async function main(argv) {
  const options = {format: "4:5,16:9", theme: "dark", out: ".films", fonts: process.env.TIN_FILM_FONTS || null};
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
      const {dir, report} = await shoot(film, {format, theme: options.theme, out: options.out, fonts});
      const {errors, warnings, blocked} = report.issues;
      console.log(`${film.name} ${format}: ${report.frames} frames, ${report.seconds.toFixed(1)}s → ${dir}`);
      for (const line of errors) console.log(`  error: ${line}`);
      for (const line of warnings) console.log(`  warning: ${line}`);
      if (blocked.length) console.log(`  blocked: ${blocked.join(", ")}`);
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
