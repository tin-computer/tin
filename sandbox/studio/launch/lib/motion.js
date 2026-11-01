// Motion library for seek(t) films. Everything is a pure function of time.
(function (root) {
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, u) => a + (b - a) * u;

  // Closed-form damped spring 0 -> 1 (k stiffness, d damping). Pure in t, so frame 812 needs no history.
  function spring(t, k = 170, d = 26) {
    if (t <= 0) return 0;
    const w0 = Math.sqrt(k), z = d / (2 * w0);
    if (z < 1) {
      const wd = w0 * Math.sqrt(1 - z * z);
      return 1 - Math.exp(-z * w0 * t) * (Math.cos(wd * t) + (z * w0 / wd) * Math.sin(wd * t));
    }
    return 1 - Math.exp(-w0 * t) * (1 + w0 * t);
  }
  // Presets: snappy UI, default containers, heavy type/logos, playful overshoot.
  const SPRING = { snappy: [320, 30], base: [170, 26], heavy: [90, 19], playful: [220, 14] };
  const sp = (t, name = 'base') => spring(t, ...SPRING[name]);

  // A value that changes target several times: sum one spring per change. keys: [[time, value], ...]
  function track(t, keys, k = 170, d = 26) {
    let v = keys[0][1];
    for (let i = 1; i < keys.length; i++) v += (keys[i][1] - keys[i - 1][1]) * spring(t - keys[i][0], k, d);
    return v;
  }
  // Text inside a morphing box: enters after the morph starts, leaves before the next one.
  const swapAlpha = (t, tIn, tOut) => Math.min(clamp((t - tIn - 0.08) / 0.12), clamp((tOut - 0.1 - t) / 0.1));

  // Easing for the rare place a spring is wrong (camera drifts, wipes).
  const ease = {
    inOut: u => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2),
    out: u => 1 - Math.pow(1 - u, 3),
    in: u => u * u * u,
    expoOut: u => (u >= 1 ? 1 : 1 - Math.pow(2, -10 * u)),
  };
  // Progress of a move that starts at t0 and lasts dur, eased.
  const P = (t, t0, dur, fn = ease.inOut) => fn(clamp((t - t0) / dur));

  // Seeded noise. Never Math.random: the render must be identical every run.
  function rng(seed) {
    return () => {
      seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
      let r = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
      return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
    };
  }
  // Typewriter: how many characters of text are visible at t, typed from t0 at cps chars/sec.
  const typed = (text, t, t0, cps = 28) => text.slice(0, Math.max(0, Math.floor((t - t0) * cps)));

  const api = { clamp, lerp, spring, SPRING, sp, track, swapAlpha, ease, P, rng, typed };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.Motion = api;
})(typeof window !== 'undefined' ? window : globalThis);
