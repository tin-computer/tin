// A desk film: several windows on one desktop, filmed as one take in scenes. A browser window
// shows a website or Tin's dashboard, a terminal window shows a shell and Claude Code, and the
// camera, the cursor and the captions work across them as in a one-window film (film.js). Each
// window is an iframe with its own document and clock; the desk scales a window's page (the
// dashboard is laid out at 1440 px and shown at 1.25x) and keeps every rect in the desk's px.
//
//   npm run film:desk -- accelerator-demo [--pace full|brisk|site=brisk,people=full] [--out .films]
//
// Scenes play in order in one take. Each has a pace: `full` as written, `brisk` with holds cut
// and optional beats and captions left out. Each scene's frames also land in their own segment,
// so a sheet can show every scene's length at both paces. See docs/feature-films.md.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {pathToFileURL} from "node:url";
import {chromium} from "playwright";
import {describe, summary} from "./checks.js";
import {player} from "./spec.js";
import {installVirtualClock} from "./clock.js";
import {BACKDROP, Director, FPS, avatar, ffmpeg, loadFonts, motionStrips, quiet, srt, stills} from "./film.js";
import {installScreen, installStage} from "./overlay.js";

const STAGE = "/__film/desk";
const TERMINAL = "/__film/terminal";
// The shot, and the desk inside it: the picture above the caption band is the desk at rest, one
// desk px to one output px.
export const DESK = {width: 1920, height: 1080, band: 140};
const PICTURE = DESK.height - DESK.band;
// Each window: where it sits on the desk, its title bar, and the page inside it (laid out at
// `page` px and shown at `scale`).
export const WINDOWS = {
  browser: {left: 60, top: 60, width: 1800, height: 820, bar: 45, page: {width: 1440, height: 620}, scale: 1.25},
  terminal: {left: 250, top: 95, width: 1420, height: 750, bar: 44, page: {width: 1420, height: 706}, scale: 1},
};
const PANES = {site: "browser", app: "browser", term: "terminal"};

export class DeskDirector extends Director {
  constructor(options) {
    super({...options, screen: {width: WINDOWS.browser.width, height: WINDOWS.browser.height}, margin: {left: WINDOWS.browser.left, top: WINDOWS.browser.top}});
    this.frames = options.frames;
    this.frame = {x: 0, y: 0, width: DESK.width, height: DESK.height};
    this.pace = "full";
    this.windows = {browser: {lift: 1, drop: 0, opacity: 1, dim: 0, shown: true}, terminal: {lift: 0.97, drop: 16, opacity: 0, dim: 0, shown: false}};
    this.front = "browser";
    this.term = new Terminal(this);
    this.scenes = [];
    this.focus("site");
  }

  // Every window's page, by its iframe's id, for the probe.
  pages() { return this.frames; }
  // The site and the dashboard; the terminal is the film's own drawing of Claude Code.
  products() { return ["site", "app"]; }

  // The window the director works in: its page's frame, where its page sits on the desk and at
  // what scale. Rects, clicks and marks go through it.
  focus(pane) {
    const win = WINDOWS[PANES[pane]];
    this.pane = pane;
    this.app = this.frames[pane];
    this.origin = {x: win.left, y: win.top + win.bar};
    this.windowScale = win.scale;
    this.margin = {left: win.left, top: win.top};
    this.screen = {width: win.width, height: win.height};
  }

  onStage(box) {
    if (!box) return box;
    const s = this.windowScale;
    const out = {...box, x: this.origin.x + box.x * s, y: this.origin.y + box.y * s};
    if ("width" in box) Object.assign(out, {width: box.width * s, height: box.height * s});
    return out;
  }

  onScreen(box) {
    if (!box) return box;
    const s = this.windowScale;
    const out = {...box, x: (box.x - this.origin.x) / s, y: (box.y - this.origin.y) / s};
    if ("width" in box) Object.assign(out, {width: box.width / s, height: box.height / s});
    return out;
  }

  async rect(target, text = false) {
    const box = await super.rect(target, text);
    if (process.env.FILM_DEBUG) console.log("rect", this.pane, JSON.stringify(target).slice(0, 60), JSON.stringify(box), JSON.stringify(this.picture));
    return box;
  }

  // A pace for the scene: `brisk` cuts holds to `brisk` seconds (or 60% of `full`).
  get brisk() { return this.pace === "brisk"; }
  async wait(full, brisk = full * 0.6) { await this.hold(this.brisk ? brisk : full); }
  // A caption the brisk cut leaves out unless it is marked to stay.
  async say(text, {brisk = false, delay = 0} = {}) {
    if (this.brisk && !brisk && text) return;
    await this.read();
    this.caption(text, {delay});
  }

  // Hold until the caption showing has been up long enough to read: 0.8 s plus 15 characters a
  // second, so a faster pace shows fewer captions and never flashes one.
  async read() {
    const current = this.captions.at(-1);
    if (!current || current.end !== null || !current.text) return;
    const need = Math.round((0.8 + current.text.length / 15) * FPS);
    const shown = this.n - current.start;
    if (shown < need) await this.hold((need - shown) / FPS);
  }

  // The browser opens a page on the dashboard by its own route, as a click on a link would.
  async open(route, {ready} = {}) {
    await this.frames.app.evaluate(to => {
      history.pushState(null, "", to);
      dispatchEvent(new PopStateEvent("popstate"));
    }, `${route}${route.includes("?") ? "&" : "?"}project=${this.world.project}`);
    if (ready) await this.ready(ready);
  }

  // Scroll the page in the focused window until a target sits `offset` px under its top, eased
  // over `duration` (the dashboard scrolls the whole document).
  async scrollPage(target, {duration = 1.0, offset = 24} = {}) {
    const [from, to] = await this.app.evaluate(([selector, gap]) => {
      const element = document.querySelector(selector);
      const top = window.scrollY + element.getBoundingClientRect().top - gap;
      const most = document.scrollingElement.scrollHeight - window.innerHeight;
      return [window.scrollY, Math.max(0, Math.min(top, most))];
    }, [target, offset]);
    const frames = Math.max(1, Math.round(duration * FPS));
    const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
    this.events.push({label: "scroll", start: this.n, end: this.n + frames});
    let i = 0;
    await this.run(async () => {
      const y = from + (to - from) * ease(++i / frames);
      await this.app.evaluate(y => window.scrollTo(0, y), y);
      return i >= frames;
    });
  }

  // What the browser shows: the website or the dashboard, and the address in its bar.
  async show(pane, address) {
    await this.page.evaluate(([pane, address]) => window.__desk.show(pane, address), [pane, address]);
  }
  async address(text) { await this.page.evaluate(text => window.__desk.address(text), text); }

  // The end card: the window in front fades out, then the logotype and the install line rise,
  // 0.5 s and 0.7 s on the settle curve.
  async endCard() {
    const settle = t => 1 - (1 - t) ** 3;
    this.events.push({label: "end card", start: this.n, end: this.n + 36});
    for (const name of Object.keys(this.windows)) if (name !== this.front) this.windows[name].shown = false;
    let i = 0;
    await this.run(() => {
      this.windows[this.front].opacity = 1 - settle(++i / 15);
      return i >= 15;
    });
    this.windows[this.front].shown = false;
    i = 0;
    await this.run(async () => {
      await this.page.evaluate(t => window.__desk.end(t), settle(++i / 21));
      return i >= 21;
    });
  }

  // One window comes forward and the other sinks back and dims, as switching to a window does: the
  // terminal rises in front of the browser, and leaves from in front of it when the browser comes
  // back. 0.6 s on the settle curve; a window coming in is opaque within its first third.
  async raise(name, {duration = 0.6} = {}) {
    const other = name === "browser" ? "terminal" : "browser";
    if (this.front === name) return;
    const frames = Math.round(duration * FPS);
    const from = {[name]: {...this.windows[name]}, [other]: {...this.windows[other]}};
    const leaving = this.windows[name].shown;
    this.windows[name].shown = true;
    this.front = name;
    if (!leaving) await this.page.evaluate(front => window.__desk.front(front), name);
    this.events.push({label: `raise ${name}`, start: this.n, end: this.n + frames});
    const settle = t => 1 - (1 - t) ** 3;
    const lerp = (a, b, t) => a + (b - a) * t;
    let i = 0;
    await this.run(() => {
      const t = settle(++i / frames);
      const near = this.windows[name], far = this.windows[other];
      if (leaving) {
        // The one in front leaves: it fades and drops a little as the one behind comes up.
        Object.assign(near, {lift: lerp(from[name].lift, 1, t), drop: 0, opacity: 1, dim: lerp(from[name].dim, 0, t)});
        Object.assign(far, {lift: lerp(1, 0.98, t), drop: lerp(0, 12, t), opacity: 1 - t, dim: 0});
      } else {
        Object.assign(near, {lift: lerp(0.97, 1, t), drop: lerp(16, 0, t), opacity: Math.min(1, (i / frames) * 3), dim: 0});
        Object.assign(far, {lift: lerp(from[other].lift, 0.96, t), drop: 0, opacity: 1, dim: lerp(from[other].dim, 0.55, t)});
      }
      return i >= frames;
    });
    if (leaving) {
      this.windows[other].shown = false;
      await this.page.evaluate(front => window.__desk.front(front), name);
    }
  }

  async tick() {
    for (const frame of new Set(Object.values(this.frames))) {
      await frame.evaluate(() => window.__film?.clock?.step(1));
      await quiet(frame);
    }
    await this.page.evaluate(windows => window.__desk.windows(windows), this.windows);
  }

  // A scene: a name for the sheet and the segments, its pace, and its own camera rules.
  async scene(name, pace, play) {
    this.pace = pace;
    const start = this.n;
    const moves = this.moves.length, journeys = this.journeys.length;
    await play();
    await this.read();
    this.caption(null);
    this.check(name, start);
    this.scenes.push({name, pace, start, end: this.n});
    // Stageshow's rules hold per scene: one push-in, a few moves, no detours.
    const film = {moves: this.moves, journeys: this.journeys};
    this.moves = this.moves.slice(moves);
    this.journeys = this.journeys.slice(journeys);
    const before = this.issues.warnings.length;
    this.judgeCamera();
    this.judgeHand();
    for (let i = before; i < this.issues.warnings.length; i++) this.issues.warnings[i] = `${name}: ${this.issues.warnings[i]}`;
    this.moves = film.moves;
    this.journeys = film.journeys;
  }
}

// The terminal window's screen: lines of spans, a shell prompt and Claude Code's input box. The
// director types, pastes and prints into it frame by frame.
class Terminal {
  constructor(director) {
    this.f = director;
    this.lines = [];
    this.input = null;
  }
  async paint() {
    await this.f.frames.term.evaluate(([lines, input]) => window.__terminal.paint(lines, input), [this.lines, this.input]);
  }
  // A shell prompt waiting for input.
  async prompt(cwd = "~/projects/aupilot") {
    this.input = {kind: "shell", prefix: `${cwd} $ `, text: "", caret: true};
    await this.paint();
  }
  // Claude Code's input box, under its header.
  async box(placeholder = "") {
    this.input = {kind: "claude", text: "", placeholder, caret: true};
    await this.paint();
  }
  // Keys at a person's speed: about 14 a second, quicker at a brisk pace.
  async type(text, {rate = 14} = {}) {
    const per = 1 / (this.f.brisk ? rate * 1.8 : rate);
    for (const character of text) {
      this.input.text += character;
      await this.paint();
      await this.f.hold(per);
    }
  }
  // A fresh screen, as when Claude Code starts; the next line paints it.
  clear() {
    this.lines = [];
  }
  async paste(text) {
    this.input.text += text;
    await this.paint();
  }
  // The typed line joins the screen as the prompt showed it.
  async enter() {
    const {kind, prefix, text} = this.input;
    this.lines.push(kind === "shell" ? [[prefix, "dim"], [text, ""]] : [["> ", "dim"], [text, ""]]);
    if (kind === "claude") this.lines.push([]);
    this.input = null;
    await this.paint();
  }
  // Lines arrive one after another over `over` seconds (a stream), or at once.
  async print(lines, {over = 0} = {}) {
    const each = lines.length > 1 ? over / lines.length : 0;
    for (const line of lines) {
      this.lines.push(line);
      await this.paint();
      if (each) await this.f.hold(each);
    }
  }
  // Claude Code at work: its spinner counts the real seconds while the film runs `film` seconds
  // of them (a time-lapse that says how long it really took).
  async work(real, film, {verb = "Working"} = {}) {
    const frames = Math.max(1, Math.round((this.f.brisk ? film * 0.6 : film) * FPS));
    const glyphs = ["·", "✢", "✳", "✶", "✻", "✽"];
    for (let i = 0; i < frames; i++) {
      const seconds = Math.round((real * (i + 1)) / frames);
      this.input = {kind: "spinner", text: `${glyphs[Math.floor(i / 4) % glyphs.length]} ${verb}… (${seconds}s · esc to interrupt)`};
      await this.paint();
      await this.f.shoot();
    }
    this.input = null;
    await this.paint();
  }
}

// The terminal's page: Geist Mono at 20/30 on near-black, the theme's warm greys, Claude's
// orange for its mark and the moss green for done.
function terminalPage(fonts) {
  return `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/assets/app.css">${fonts ? '<link rel="stylesheet" href="/film-fonts/fonts.css">' : ""}
<style>
  html, body { margin: 0; height: 100%; background: #0e0d0c !important; overflow: hidden; }
  #screen { padding: 26px 36px; font: 400 20px/30px "Geist Mono", Menlo, monospace; color: #e9e3d7; white-space: pre; font-variant-ligatures: none; }
  .dim { color: #857c6f; } .soft { color: #b3aa9b; } .claude { color: #d97757; } .ok { color: #99af62; } .warn { color: #d9a441; }
  .bold { font-weight: 600; } .link { color: #99af62; text-decoration: underline; text-underline-offset: 4px; cursor: pointer; }
  .rule { color: #3a3631; display: block; overflow: hidden; }
  .caret { display: inline-block; width: 11px; height: 24px; vertical-align: -5px; background: #e9e3d7; animation: blink 1.06s steps(1) infinite; }
  .placeholder { color: #5f594c; }
  /* Lines leave under the top margin whole, as a terminal scrolls by lines. */
  body::before { content: ""; position: fixed; left: 0; right: 0; top: 0; height: 26px; background: #0e0d0c; z-index: 1; }
  @keyframes blink { 50% { opacity: 0; } }
</style></head><body><div id="screen"></div>
<script>
  const esc = text => text.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]));
  const span = ([text, cls]) => cls === "link" ? '<a class="link" href="#" onclick="return false">' + esc(text) + '</a>' : cls ? '<span class="' + cls + '">' + esc(text) + '</span>' : esc(text);
  const RULE = "─".repeat(140);
  window.__terminal = {
    paint(lines, input) {
      const body = lines.map(line => line.map(span).join("")).join("\\n");
      let tail = "";
      if (input?.kind === "shell") tail = esc(input.prefix).replace(/^(.*)$/, '<span class="dim">$1</span>') + esc(input.text) + (input.caret ? '<span class="caret"></span>' : "");
      if (input?.kind === "claude") tail = '<span class="rule">' + RULE + '</span>' + '<span class="dim">❯ </span>' + (input.text ? esc(input.text) : '<span class="placeholder">' + esc(input.placeholder || "") + '</span>') + (input.caret ? '<span class="caret"></span>' : "") + '\\n<span class="rule">' + RULE + '</span>';
      if (input?.kind === "spinner") tail = '<span class="claude">' + esc(input.text.slice(0, 1)) + '</span>' + '<span class="soft">' + esc(input.text.slice(1)) + '</span>';
      document.getElementById("screen").innerHTML = body + (body && tail ? "\\n" : "") + tail;
      // A terminal keeps its newest line in view: once the screen is full, older lines go up and out.
      const screen = document.getElementById("screen");
      screen.style.transform = "";
      const over = screen.getBoundingClientRect().bottom - window.innerHeight;
      if (over > 0) screen.style.transform = "translateY(" + -Math.ceil(over / 30) * 30 + "px)";
    },
  };
</script></body></html>`;
}

// The desk: black, with the browser and the terminal as windows. A window's title bar has muted
// traffic lights; the browser's has its address in the middle. Paper: Films · Accelerator demo.
function deskPage({fonts, site, app, theme}) {
  const b = WINDOWS.browser, t = WINDOWS.terminal;
  const lights = '<i></i><i></i><i></i>';
  const lock = '<svg width="11" height="13" viewBox="0 0 11 13" fill="none"><rect x="1" y="5.5" width="9" height="7" rx="1.5" fill="#857C6F"/><path d="M3 5.5V4a2.5 2.5 0 0 1 5 0v1.5" stroke="#857C6F" stroke-width="1.4"/></svg>';
  return `<!doctype html><html data-theme="${theme}"><head><meta charset="utf-8">
<link rel="stylesheet" href="/assets/app.css">${fonts ? '<link rel="stylesheet" href="/film-fonts/fonts.css">' : ""}
<style>
  html, body { margin: 0 !important; padding: 0 !important; width: ${DESK.width / 2}px; height: ${DESK.height / 2}px; min-height: 0 !important; overflow: hidden !important; background: #000 !important; }
  #room { position: absolute; left: 0; top: 0; width: ${DESK.width}px; height: ${PICTURE}px; transform-origin: 0 0; }
  .win { position: absolute; border-radius: 12px; overflow: hidden; background: #141210; box-shadow: 0 0 0 1px rgb(255 255 255 / 9%); transform-origin: 50% 50%; }
  .win.is-terminal { background: #0e0d0c; box-shadow: 0 0 0 1px rgb(255 255 255 / 12%), 0 24px 60px rgb(0 0 0 / 60%); }
  .bar { position: relative; display: flex; align-items: center; gap: 9px; padding: 0 18px; background: #1b1917; border-bottom: 1px solid rgb(246 241 231 / 8%); }
  .is-terminal .bar { background: #1a1816; }
  .bar i { width: 13px; height: 13px; border-radius: 50%; background: #3a3631; flex: 0 0 auto; }
  .address { position: absolute; left: 620px; top: 9px; width: 560px; height: 27px; border-radius: 7px; background: #23201d; display: flex; align-items: center; justify-content: center; gap: 8px;
    font: 400 15px/20px var(--sans, sans-serif); color: #b3aa9b; }
  .title { position: absolute; left: 0; right: 0; top: 12px; text-align: center; font: 400 15px/20px var(--sans, sans-serif); color: #857c6f; }
  .page { position: absolute; left: 0; border: 0; transform-origin: 0 0; background: #141210; }
  .dimmer { position: absolute; inset: 0; background: #000; pointer-events: none; opacity: 0; }
  #end { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 56px; opacity: 0; pointer-events: none; z-index: 5; }
  #end .logotype { width: 220px; height: 141px; background: #f6f1e7; -webkit-mask: url(/assets/tin-logotype-ink.png) center / contain no-repeat; mask: url(/assets/tin-logotype-ink.png) center / contain no-repeat; }
  #end .line { display: flex; align-items: center; gap: 18px; padding: 22px 32px; border-radius: 999px; background: #141210; box-shadow: 0 0 0 1px rgb(246 241 231 / 14%);
    font: 400 24px/32px "Geist Mono", Menlo, monospace; color: #f6f1e7; white-space: pre; font-variant-ligatures: none; font-feature-settings: "liga" 0, "calt" 0; }
  #end .line span { color: #857c6f; }
</style></head><body><div id="room">
  <div class="win is-browser" id="browser" style="left:${b.left}px;top:${b.top}px;width:${b.width}px;height:${b.height}px;z-index:1">
    <div class="bar" style="height:${b.bar}px">${lights}<div class="address">${lock}<span id="address">tin.computer</span></div></div>
    <iframe class="page" id="site" src="${site}" style="top:${b.bar}px;width:${b.page.width}px;height:${b.page.height}px;transform:scale(${b.scale})"></iframe>
    <iframe class="page" id="app" src="${app}" style="top:${b.bar}px;width:${b.page.width}px;height:${b.page.height}px;transform:scale(${b.scale});visibility:hidden"></iframe>
    <div class="dimmer"></div>
  </div>
  <div class="win is-terminal" id="terminal" style="left:${t.left}px;top:${t.top}px;width:${t.width}px;height:${t.height}px;z-index:0;visibility:hidden">
    <div class="bar" style="height:${t.bar}px">${lights}<div class="title">aupilot — claude</div></div>
    <iframe class="page" id="term" src="${TERMINAL}" style="top:${t.bar}px;width:${t.page.width}px;height:${t.page.height}px"></iframe>
    <div class="dimmer"></div>
  </div>
  <div id="end"><div class="logotype"></div><div class="line"><span>$</span>claude mcp add --transport http tin https://app.tin.computer/mcp</div></div>
</div>
<script>
  window.__desk = {
    show(pane, address) {
      document.getElementById("site").style.visibility = pane === "site" ? "visible" : "hidden";
      document.getElementById("app").style.visibility = pane === "app" ? "visible" : "hidden";
      if (address) document.getElementById("address").textContent = address;
    },
    address(text) { document.getElementById("address").textContent = text; },
    end(t) {
      const card = document.getElementById("end");
      card.style.opacity = String(t);
      card.style.transform = "translateY(" + (1 - t) * 12 + "px)";
    },
    front(name) {
      document.getElementById(name).style.zIndex = 2;
      document.getElementById(name === "browser" ? "terminal" : "browser").style.zIndex = 1;
    },
    // Each window's lift (its scale), drop (px down), opacity and dimming.
    windows(state) {
      for (const [name, win] of Object.entries(state)) {
        const element = document.getElementById(name);
        element.style.visibility = win.shown ? "visible" : "hidden";
        element.style.opacity = String(win.opacity);
        element.style.transform = "translateY(" + win.drop + "px) scale(" + win.lift + ")";
        element.querySelector(".dimmer").style.opacity = String(win.dim);
      }
    },
  };
</script></body></html>`;
}

// Pages from the open web (the website the film opens on), kept the first time and served from
// .films/cache after, so every take is the same. Framing headers are left off: the page is shown
// in the desk's window.
function cached(dir) {
  fs.mkdirSync(dir, {recursive: true});
  return async route => {
    const request = route.request();
    if (request.method() !== "GET") return route.abort();
    const key = crypto.createHash("sha1").update(request.url()).digest("hex");
    const file = path.join(dir, key);
    if (!fs.existsSync(file)) {
      try {
        const response = await route.fetch();
        const headers = Object.fromEntries(Object.entries(response.headers()).filter(([name]) => !/^(x-frame-options|content-security-policy|content-encoding|content-length|transfer-encoding|set-cookie)$/i.test(name)));
        fs.writeFileSync(`${file}.json`, JSON.stringify({status: response.status(), headers}));
        fs.writeFileSync(file, await response.body());
      } catch {
        return route.abort();
      }
    }
    const meta = JSON.parse(fs.readFileSync(`${file}.json`, "utf8"));
    return route.fulfill({status: meta.status, headers: meta.headers, body: fs.readFileSync(file)});
  };
}

// Film a desk film: serve its world, open the desk, play its scenes in order, encode.
export async function shootDesk(film, {pace = {}, theme = "dark", out = ".films", fonts = null, until = null, check = false} = {}) {
  const {serve, siteUrl, start, member = "Avery", ready = "main"} = film.desk;
  const now = Date.parse(film.now);
  const served = await serve({now: () => new Date(now)});
  const browser = await chromium.launch({headless: true});
  const issues = {errors: [], warnings: [], blocked: []};
  // Each pace keeps its own take: full, brisk, or a mix named by its brisk scenes.
  const briskScenes = Object.entries(pace).filter(([name, value]) => name !== "all" && value === "brisk").map(([name]) => name);
  const label = pace.all === "brisk" ? "brisk" : briskScenes.length ? `brisk-${briskScenes.join("-")}` : "full";
  const dir = path.join(out, film.name, `16x9-${label}`);
  fs.rmSync(dir, {recursive: true, force: true});
  fs.mkdirSync(path.join(dir, "segments"), {recursive: true});
  const mp4 = path.join(dir, "film.mp4");
  const encode = file => ffmpeg(["-f", "image2pipe", "-framerate", String(FPS), "-c:v", "png", "-i", "-",
    "-vf", `scale=${DESK.width}:${DESK.height}:flags=lanczos,format=yuv420p`, "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", "-an", file]);
  let encoder = encode(mp4);
  try {
    const deviceScale = 2;
    const view = {width: DESK.width / deviceScale, height: DESK.height / deviceScale};
    const context = await browser.newContext({viewport: view, deviceScaleFactor: deviceScale, colorScheme: theme, timezoneId: film.timezone || "UTC", locale: "en-US"});
    const local = new URL(served.base).origin;
    const site = new URL(siteUrl).origin;
    await context.grantPermissions(["clipboard-read", "clipboard-write"], {origin: site});
    const web = cached(path.join(out, "cache", "web"));
    await context.route("**/*", async route => {
      const url = route.request().url();
      if (url.startsWith(site) || (!url.startsWith(local) && route.request().frame()?.url().startsWith(site))) {
        // The website and what it loads, except what only measures visits.
        if (/vercel-insights|_vercel\/insights|_vercel\/speed-insights|posthog|plausible|google-analytics|googletagmanager|tin\.computer\/(?:sdk|edge)|edge\.tin\.computer/.test(url)) return route.abort();
        return web(route);
      }
      if (!url.startsWith(local)) {
        issues.blocked.push(new URL(url).origin);
        return route.abort();
      }
      const {pathname} = new URL(url);
      if (pathname === STAGE) return route.fulfill({body: deskPage({fonts: Boolean(fonts), site: siteUrl, app: `${start}?project=${film.desk.project}`, theme}), contentType: "text/html"});
      if (pathname === TERMINAL) return route.fulfill({body: terminalPage(Boolean(fonts)), contentType: "text/html"});
      if (decodeURIComponent(pathname).startsWith("/{{CLERK_FRONTEND_API_URL}}/")) return route.fulfill({body: "", contentType: "text/javascript"});
      if (pathname.startsWith("/film-fonts/") && fonts?.has(path.basename(pathname))) {
        return route.fulfill({body: fonts.get(path.basename(pathname)), contentType: pathname.endsWith(".css") ? "text/css" : "font/woff2"});
      }
      if (fonts && route.request().resourceType() === "document") {
        const response = await route.fetch();
        const html = (await response.text()).replace("<!--PRIVATE_FONTS_STYLESHEET-->", '<link rel="stylesheet" href="/film-fonts/fonts.css" />');
        return route.fulfill({response, body: html});
      }
      return route.continue();
    });
    await context.addInitScript(`(${installVirtualClock})(${JSON.stringify({fps: FPS, epoch: now})});`);
    await context.addInitScript(`(${installScreen})();`);
    await context.addInitScript(`(${installStage})(${JSON.stringify({backdrop: BACKDROP.dark, path: STAGE, size: DESK, dpr: deviceScale})});`);
    await context.addInitScript(({member, theme, local, image}) => {
      if (location.origin !== local) return;
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: member, lastName: "Lane", fullName: `${member} Lane`, imageUrl: image}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
      localStorage.setItem("tin-lite:theme", theme);
    }, {member, theme, local, image: avatar(member)});
    const page = await context.newPage();
    page.on("pageerror", error => issues.errors.push(`The page threw: ${error.message}`));
    await page.goto(`${served.base}${STAGE}`, {waitUntil: "domcontentloaded"});
    const frames = {};
    for (const id of ["site", "app", "term"]) frames[id] = await (await page.waitForSelector(`#${id}`, {state: "attached"})).contentFrame();
    await Promise.all(Object.values(frames).map(frame => frame.waitForLoadState("domcontentloaded")));
    const director = new DeskDirector({page, frames, format: "16:9", size: DESK, room: {width: DESK.width, height: PICTURE}, deviceScale, served: {...served, project: film.desk.project}, issues, ripple: false, check});
    let segment = null;
    director.sink = async png => {
      for (const target of [encoder, segment].filter(Boolean)) {
        if (!target.child.stdin.write(png)) await new Promise(resolve => target.child.stdin.once("drain", resolve));
      }
    };
    // The desk boots without being filmed: the website and the dashboard load, fonts arrive.
    director.recording = false;
    for (let i = 0; i < 20 * FPS && !(await frames.app.locator(ready).count()); i++) await director.shoot();
    for (let i = 0; i < FPS; i++) await director.shoot();
    for (const frame of Object.values(frames)) await frame.evaluate(() => document.fonts.ready);
    await page.evaluate(() => document.fonts.ready);
    director.recording = true;

    const paceOf = name => pace[name] || pace.all || "full";
    for (const [index, scene] of film.scenes.entries()) {
      segment = encode(path.join(dir, "segments", `${String(index + 1).padStart(2, "0")}-${scene.name}-${paceOf(scene.name)}.mp4`));
      await director.scene(scene.name, paceOf(scene.name), () => scene.play(director));
      segment.child.stdin.end();
      await segment.done;
      segment = null;
      // A rehearsal can stop after a scene.
      if (until === scene.name) break;
    }
    director.caption(null);
    director.judgeLens();
    director.judgeText();
    await context.close();

    issues.blocked = [...new Set(issues.blocked)];
    if (!director.marks.poster) issues.errors.push("The film never marks its poster.");
    encoder.child.stdin.end();
    await encoder.done;
    encoder = null;
    await stills(director, DESK, dir);
    await motionStrips(director, DESK, dir);
    fs.writeFileSync(path.join(dir, "captions.srt"), srt(director.captions, director.n));
    const scenes = director.scenes.map(item => ({...item, seconds: Math.round(((item.end - item.start) / FPS) * 10) / 10}));
    const report = {film: film.name, title: film.title, size: DESK, frames: director.n, seconds: director.n / FPS, scenes, captions: director.captions, hashes: director.hashes, issues};
    if (director.checking) report.checks = director.findings.map(describe);
    if (director.checking) report.summary = summary(director.findings);
    fs.writeFileSync(path.join(dir, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
    return {dir, report};
  } catch (error) {
    for (const target of [encoder].filter(Boolean)) {
      if (target.child.exitCode === null) {
        target.child.stdin.destroy();
        target.child.kill("SIGKILL");
        await target.done.catch(() => {});
      }
    }
    fs.rmSync(mp4, {force: true});
    throw error;
  } finally {
    await browser.close();
    served.server.close();
  }
}

async function main(argv) {
  const options = {pace: "full", out: ".films", fonts: process.env.TIN_FILM_FONTS || null};
  const names = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) options[argv[i].slice(2)] = argv[++i];
    else names.push(argv[i]);
  }
  // --pace brisk, or per scene: --pace site=brisk,people=full (the rest full).
  const pace = options.pace.includes("=") ? Object.fromEntries(options.pace.split(",").map(item => item.split("="))) : {all: options.pace};
  const fonts = await loadFonts(options.fonts);
  for (const name of names) {
    const file = path.resolve("web/film/films", `${name}.js`);
    const module = await import(pathToFileURL(file));
    // Each scene is code (`play`) or data (`beats`, with the film's named `values`).
    const scenes = module.scenes.map(scene => ({...scene, play: player(scene, module.values, `scene ${scene.name}`)}));
    const film = {name, ...module, scenes};
    const {dir, report} = await shootDesk(film, {pace, out: options.out, fonts, until: options.until || null, check: options.check === "on"});
    console.log(`${film.name}: ${report.frames} frames, ${report.seconds.toFixed(1)}s → ${dir}`);
    for (const scene of report.scenes) console.log(`  ${scene.name} (${scene.pace}): ${scene.seconds}s`);
    for (const line of report.issues.errors) console.log(`  error: ${line}`);
    for (const line of report.issues.warnings) console.log(`  warning: ${line}`);
    if (report.issues.blocked.length) console.log(`  blocked: ${report.issues.blocked.join(", ")}`);
    for (const line of report.summary ?? []) console.log(`  check: ${line}`);
    if (report.issues.errors.length) process.exitCode = 1;
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
  });
}
