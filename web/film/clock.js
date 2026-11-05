// The film clock. Injected into the page before any of the dashboard's code, it replaces the
// page's clocks with a virtual one that moves only when the recorder steps it, by exactly 1/fps:
// timers fire in time order, one round of animation frames runs, and every CSS transition and
// animation is moved to the new time. Each frame of a film is then the same on every render.
//
// Adapted from Stageshow's kit/clock.ts (MIT, Copyright (c) 2026 Benji Taylor; see
// third_party/licenses/stageshow-LICENSE.txt), without its live-playback and layout-frame parts.
// The function must stay self-contained: the recorder injects its source as an init script.
export function installVirtualClock({fps = 30, epoch = Date.now()} = {}) {
  const w = window;
  if (w.__film?.clock) return w.__film.clock;
  const RealDate = Date;
  // Not 0: code that tests a timestamp for truthiness must still see one.
  const BASE = 1000;
  let vt = BASE;
  let frame = 0;

  const timers = new Map();
  let nextId = 1;
  let seq = 0;
  let frameCallbacks = new Map();
  const arm = (fn, ms, args, every) => {
    const delay = Math.max(0, Number(ms) || 0);
    const id = nextId++;
    timers.set(id, {id, at: vt + delay, seq: seq++, fn, args, every: every ? Math.max(1, delay) : null});
    return id;
  };
  w.setTimeout = (fn, ms, ...args) => arm(fn, ms, args, false);
  w.setInterval = (fn, ms, ...args) => arm(fn, ms, args, true);
  w.clearTimeout = w.clearInterval = id => void timers.delete(id);
  w.requestAnimationFrame = fn => {
    const id = nextId++;
    frameCallbacks.set(id, fn);
    return id;
  };
  w.cancelAnimationFrame = id => void frameCallbacks.delete(id);
  performance.now = () => vt;
  const dateNow = () => Math.floor(epoch + (vt - BASE));
  const VirtualDate = function (...a) {
    if (!new.target) return new RealDate(dateNow()).toString();
    return a.length ? new RealDate(...a) : new RealDate(dateNow());
  };
  Object.assign(VirtualDate, {prototype: RealDate.prototype, now: dateNow, parse: RealDate.parse, UTC: RealDate.UTC});
  w.Date = VirtualDate;

  // Queued work (promise chains, renders after a fetch) runs between tasks; a few message round
  // trips let it land before time moves on.
  const tick = () => new Promise(resolve => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => {
      channel.port1.close();
      channel.port2.close();
      resolve();
    };
    channel.port2.postMessage(0);
  });
  const run = (fn, args) => {
    try {
      if (typeof fn === "function") fn(...args);
    } catch (error) {
      console.error(error);
    }
  };

  // CSS transitions and animations: each is held (playback rate 0) from the moment it is first
  // seen, and its own clock is moved by hand with the virtual one. One that reaches its end is
  // finished, so transitionend and animationend still fire.
  const held = new WeakMap();
  const live = new Set();
  const scan = () => {
    for (const animation of document.getAnimations()) {
      if (held.has(animation) || animation.playState === "finished" || animation.playState === "idle") continue;
      held.set(animation, {local: 0, rate: animation.playbackRate || 1, born: vt});
      animation.playbackRate = 0;
      animation.currentTime = 0;
      live.add(animation);
    }
  };
  const settle = async () => {
    for (let i = 0; i < 4; i++) await tick();
    scan();
  };
  const moveAnimations = from => {
    scan();
    for (const animation of [...live]) {
      const state = held.get(animation);
      if (animation.playState === "idle" || animation.playState === "finished") {
        live.delete(animation);
        continue;
      }
      if (animation.playState !== "paused") state.local += (vt - Math.max(from, state.born)) * state.rate;
      const end = animation.effect?.getComputedTiming().endTime;
      if (typeof end === "number" && Number.isFinite(end) && state.local >= end) {
        // Cancel a pending play task before restoring the rate, or a delayed transition can
        // restart on the native timeline after finish().
        animation.pause();
        animation.playbackRate = state.rate;
        animation.finish();
        live.delete(animation);
      } else {
        animation.currentTime = state.local;
      }
    }
  };

  const stepTo = async to => {
    const from = vt;
    await settle();
    // Timers due by `to`, in time order, each with the work it queued. A hair of slack, so a wait
    // summed from frame-sized steps can't miss its frame by a rounding error.
    for (let guard = 0; guard < 10000; guard++) {
      let next = null;
      for (const timer of timers.values()) {
        if (timer.at <= to + 1e-6 && (!next || timer.at < next.at || (timer.at === next.at && timer.seq < next.seq))) next = timer;
      }
      if (!next) break;
      vt = next.at;
      if (next.every === null) timers.delete(next.id);
      else Object.assign(next, {at: next.at + next.every, seq: seq++});
      run(next.fn, next.args);
      await settle();
    }
    vt = to;
    frame++;
    const callbacks = frameCallbacks;
    frameCallbacks = new Map();
    for (const fn of callbacks.values()) run(fn, [vt]);
    await settle();
    moveAnimations(from);
  };

  const control = {
    fps,
    get frame() { return frame; },
    now: () => vt,
    // Exact frame times, without accumulating rounded deltas.
    async step(n = 1) {
      for (let i = 0; i < n; i++) await stepTo(BASE + ((frame + 1) * 1000) / fps);
    },
    settle,
  };
  w.__film = {...(w.__film || {}), clock: control};
  return control;
}
