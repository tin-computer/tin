// Named curves for a film's moves, after Stageshow's kit/ease.ts (MIT, Copyright (c) 2026 Benji
// Taylor; see third_party/licenses/stageshow-LICENSE.txt), which measured them from product launch
// films. Each is a cubic-bezier, worked out as CSS does.
export const EASE = {
  // Moves inside a shot (a push-in, a pan, a long cursor trip): peak at about 40%, long soft tail.
  house: [0.65, 0, 0.1, 1],
  // The outgoing half of a move that ends in a cut: accelerates into it.
  through: [0.8, 0, 0.1, 1],
  // The incoming half after a cut: arrives at speed, settles.
  settle: [0.05, 0.85, 0.3, 1],
  // A cursor arriving from off-frame, already moving.
  enter: [0.1, 0.8, 0.3, 0.95],
  // The camera's push-in.
  camera: [0.45, 0, 0.1, 1],
  // Travel between stable positions.
  travel: [0.45, 0, 0.1, 1],
};

export function bezier(curve) {
  const [x1, y1, x2, y2] = typeof curve === "string" ? EASE[curve] : curve;
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const x = u => ((ax * u + bx) * u + cx) * u;
  const dx = u => (3 * ax * u + 2 * bx) * u + cx;
  return t => {
    if (t <= 0) return 0;
    if (t >= 1) return 1;
    // Newton, falling back to bisection where the slope is flat.
    let u = t;
    for (let i = 0; i < 8; i++) {
      const slope = dx(u);
      if (Math.abs(slope) < 1e-6) break;
      const next = u - (x(u) - t) / slope;
      if (next < 0 || next > 1) break;
      u = next;
    }
    if (Math.abs(x(u) - t) > 1e-6) {
      let lo = 0, hi = 1;
      for (let i = 0; i < 40; i++) {
        u = (lo + hi) / 2;
        if (x(u) < t) lo = u;
        else hi = u;
      }
    }
    return ((ay * u + by) * u + cy) * u;
  };
}
