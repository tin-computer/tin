/**
 * Slow in and slow out, squash and stretch: the measurable principles of how things move.
 */
import { area } from "../film.js";
import { defineRule } from "../rule.js";
import { carried, contrast, isAncestor, parseColour, perFrame, px, runs, t, } from "./util.js";
/** Moves worth judging: a real distance and duration, by a top-level mover (not carried by a moving parent). */
const judged = (el, minDistance = 60) => el.moves().filter((m) => {
    if (m.seconds < 0.2 || m.distance < px(el.film, minDistance))
        return false;
    const s = el.at(m.from);
    if (Math.max(area(s.visible), area(s.box)) < 400)
        return false;
    const mid = Math.round((m.from + m.to) / 2);
    return !carried(el.film, mid, el.at(mid)?.raw.k ?? s.raw.k);
});
const movers = (film) => film.elements.filter((e) => e.role !== "cursor");
const strength = (m, o) => (m.seconds >= 0.8 ? o.long : m.seconds >= 0.4 ? o.medium : o.short);
export const linear = defineRule({
    id: "easing/linear",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "A move that ends on the canvas speeds up at its start or slows down at its end: its ease-in or its ease-out is at least 0.24, where 0.20 means constant speed.",
        why: "Real objects speed up and slow down, so a window or card that travels at one constant speed and stops at full speed looks mechanical. The ease-in is the share of the move's time spent covering its first 20% of distance, and the ease-out the share spent on its last 20%; both are 0.20 at constant speed. The rule checks moves of at least 0.2 s and 60 px at 1080p that end with the element still on the canvas, ignoring cursors and elements carried along by a moving parent.",
        fix: "Drive the move with an easing curve instead of `interpolate` with no `easing`: use ease-in-out for a move between two positions where the element is still, and ease-out for an element that arrives fast from off the canvas.",
    },
    check({ film, report }) {
        for (const el of movers(film))
            for (const m of judged(el))
                if (m.lands && m.easeIn < 0.24 && m.easeOut < 0.24)
                    report({
                        element: el,
                        frames: [m.from, m.to],
                        measured: `moves at nearly constant speed, with an ease-in of ${m.easeIn.toFixed(2)} and an ease-out of ${m.easeOut.toFixed(2)} over ${m.seconds.toFixed(2)} s (constant speed gives 0.20)`,
                        need: "an ease-in or an ease-out of at least 0.24",
                    });
    },
});
export const weakOut = defineRule({
    id: "easing/weak-out",
    kind: "lint",
    severity: "error",
    options: {
        long: 0.4,
        medium: 0.33,
        short: 0.26,
        floorLong: 0.3,
        floorMedium: 0.26,
        floorShort: 0.22,
    },
    docs: {
        summary: "A move that ends on the canvas has an ease-out of at least 0.40 if it lasts 0.8 s or more, 0.33 if it lasts 0.4 to 0.8 s, and 0.26 if it is shorter, where 0.20 means constant speed.",
        why: "A long move that barely slows down before it stops looks as if it was stopped by force rather than coming to rest, while a very short move needs little slowing down to look natural. The ease-out is the share of the move's time spent covering its last 20% of distance. Below the target the finding is a warning; below a lower minimum (0.30, 0.26 and 0.22 for the same three durations) it is an error. Moves at nearly constant speed are reported by `easing/linear` instead.",
        fix: "Use a stronger ease-out curve, for example a cubic bezier whose last control point is around (0.1, 1) or (0.2, 1), or give the move more time to slow down at its end.",
        options: {
            long: "The ease-out required for a move lasting 0.8 s or more (a share of the move's time, 0.20 being constant speed).",
            medium: "The ease-out required for a move lasting at least 0.4 s and under 0.8 s.",
            short: "The ease-out required for a move lasting under 0.4 s.",
            floorLong: "For a move lasting 0.8 s or more, the ease-out below which the finding is an error rather than a warning.",
            floorMedium: "For a move lasting at least 0.4 s and under 0.8 s, the ease-out below which the finding is an error rather than a warning.",
            floorShort: "For a move lasting under 0.4 s, the ease-out below which the finding is an error rather than a warning.",
        },
    },
    check({ film, options, report }) {
        for (const el of movers(film))
            for (const m of judged(el)) {
                const need = strength(m, options);
                if (!m.lands ||
                    m.easeOut >= need ||
                    (m.easeIn < 0.24 && m.easeOut < 0.24))
                    continue;
                const floor = strength(m, {
                    long: options.floorLong,
                    medium: options.floorMedium,
                    short: options.floorShort,
                });
                report({
                    element: el,
                    frames: [m.from, m.to],
                    measured: `has an ease-out of ${m.easeOut.toFixed(2)} on a ${m.seconds.toFixed(2)} s move of ${m.distance.toFixed(0)} px${m.easeOut >= floor ? " (above the error minimum, so this is a warning)" : ""}`,
                    need: `an ease-out of at least ${need.toFixed(2)} for a move of this duration`,
                    severity: m.easeOut >= floor ? "warn" : "error",
                });
            }
    },
});
export const noEaseIn = defineRule({
    id: "easing/no-ease-in",
    kind: "lint",
    severity: "warn",
    options: {},
    docs: {
        summary: "A move that starts on the canvas, ends on the canvas, lasts at least 0.4 s and averages under 750 px/s has an ease-in of at least 0.12.",
        why: "An element that goes from standing still to full speed in a single frame looks as if it was struck. Starting at full speed is acceptable when the viewer can't see the start (the element enters from off the canvas) or when the move is fast. The ease-in is the share of the move's time spent covering its first 20% of distance; it is 0.20 at constant speed and near 0 when the move starts at full speed.",
        fix: "Use an ease-in-out curve instead of an ease-out curve for a move that starts where the viewer can see the element standing still.",
    },
    check({ film, report }) {
        for (const el of movers(film))
            for (const m of judged(el)) {
                const start = el.at(m.from);
                const speed = m.distance / m.seconds;
                if (film.onCanvas(start) &&
                    m.lands &&
                    m.easeIn < 0.12 &&
                    speed < 750 &&
                    m.seconds >= 0.4)
                    report({
                        element: el,
                        frames: [m.from, m.to],
                        measured: `starts at full speed, with an ease-in of ${m.easeIn.toFixed(2)} on a move averaging ${speed.toFixed(0)} px/s`,
                        need: "an ease-in of at least 0.12",
                    });
            }
    },
});
export const abruptStop = defineRule({
    id: "easing/abrupt-stop",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "A move that ends on the canvas is slower than 120 px/s on its last moving frame, or at most 60% of its speed three moving frames earlier.",
        why: "If the last frames of a move are as fast as the frames before them, the element stops all at once, as if it hit something. The rule compares the speed on the last frame where the element moves at least 1 px with its speed three such frames earlier.",
        fix: "End the move with an ease-out curve. Also check that the move isn't cut short: an `interpolate` input range or a `Sequence` that ends before the easing curve reaches its end stops the element at full speed.",
    },
    check({ film, report }) {
        const fast = perFrame(film, 120);
        for (const el of movers(film))
            for (const m of judged(el)) {
                const moving = m.steps.filter((s) => s >= 1);
                if (!m.lands || moving.length < 4)
                    continue;
                const last = moving[moving.length - 1];
                const before = moving[moving.length - 4];
                if (last >= fast && last >= 0.6 * before)
                    report({
                        element: el,
                        frames: [m.from, m.to],
                        measured: `stops straight from ${(last * film.fps).toFixed(0)} px/s, after moving at ${(before * film.fps).toFixed(0)} px/s three frames earlier`,
                        need: "a last-frame speed under 120 px/s or at most 60% of the speed three frames earlier",
                    });
            }
    },
});
export const stutter = defineRule({
    id: "easing/stutter",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "During a move, the speed on any one frame never drops below 35% of the slower of the frames on either side when both of those are moving at 450 px/s or more.",
        why: "When the speed drops almost to zero for a frame in the middle of a fast move, the element looks like it hesitates. This usually means two animations run one after the other or are added together, for example a layout change followed by a transform, or two `interpolate` ranges that meet without overlapping.",
        fix: "Drive the whole motion from one progress value, or overlap the two animations so the speed never drops between them.",
    },
    check({ film, report }) {
        const quick = perFrame(film, 450);
        for (const el of film.elements)
            for (const m of judged(el)) {
                const s = m.steps;
                for (let i = 1; i < s.length - 1; i += 1) {
                    const side = Math.min(s[i - 1], s[i + 1]);
                    if (side >= quick && s[i] < 0.35 * side) {
                        report({
                            element: el,
                            frames: [m.from, m.to],
                            measured: `nearly stops in the middle of a move: its speed goes ${(s[i - 1] * film.fps).toFixed(0)} → ${(s[i] * film.fps).toFixed(0)} → ${(s[i + 1] * film.fps).toFixed(0)} px/s over three frames at ${t(film, m.from + i + 1)}`,
                            need: "no frame slower than 35% of the frames on either side when both move at 450 px/s or more",
                        });
                        break;
                    }
                }
            }
    },
});
export const edgeReversal = defineRule({
    id: "easing/edge-reversal",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "When a surface (a window, card or panel) covering at least 3% of the canvas moves, each of its edges that travels 40 px or more at 1080p never moves backwards by more than 12 px at 1080p or 3% of its travel, whichever is larger.",
        why: "When a surface's scale and its layout size are animated separately, an edge can first move the opposite way to its travel, or go past its final position and come back. The viewer sees a wobble that isn't in the easing curve. The rule follows the left, right, top and bottom edges of the surface's visible rectangle through each move of at least 0.2 s and 60 px at 1080p, and measures the largest distance an edge moves back against its overall direction.",
        fix: "Animate the surface's on-screen rectangle itself (x, y, width and height on one easing curve) and compute any scale or clipping from it, instead of easing scale and size separately.",
    },
    check({ film, report }) {
        for (const el of film.elements.filter((e) => e.role === "surface"))
            for (const m of judged(el)) {
                const span = [];
                for (let f = m.from; f <= m.to; f += 1)
                    if (el.at(f))
                        span.push(el.at(f).visible);
                if (!span.length || area(span[0]) < 0.03 * film.area)
                    continue;
                let worst;
                for (const [name, edge] of [
                    ["left", (b) => b[0]],
                    ["right", (b) => b[0] + b[2]],
                    ["top", (b) => b[1]],
                    ["bottom", (b) => b[1] + b[3]],
                ]) {
                    const e = span.map(edge);
                    const sign = e[e.length - 1] >= e[0] ? 1 : -1;
                    const along = e.map((v) => v * sign);
                    const travel = Math.abs(e[e.length - 1] - e[0]);
                    if (travel < px(film, 40))
                        continue;
                    let peak = -Infinity;
                    let against = 0;
                    for (const v of along) {
                        peak = Math.max(peak, v);
                        against = Math.max(against, peak - v);
                    }
                    if (against > Math.max(px(film, 12), 0.03 * travel) &&
                        (!worst || against > worst[0])) {
                        const kind = along[0] - Math.min(...along) >=
                            Math.max(...along) - along[along.length - 1]
                            ? "first moves the opposite way to its travel"
                            : "goes past its final position and comes back";
                        worst = [
                            against,
                            `the ${name} edge ${kind} by ${against.toFixed(0)} px`,
                        ];
                    }
                }
                if (worst)
                    report({
                        element: el,
                        frames: [m.from, m.to],
                        measured: worst[1],
                        need: "every edge moves in one direction, going back by no more than 12 px at 1080p or 3% of its travel",
                    });
            }
    },
});
const cursors = (film) => film.elements.filter((e) => e.role === "cursor");
export const cursorStretch = defineRule({
    id: "cursor/no-stretch",
    kind: "lint",
    severity: "error",
    options: { speed: 900, stretch: 1.06 },
    docs: {
        summary: "On frames where a cursor moves at 900 px/s or more, it is stretched to at least 1.06 along its direction of travel, on at least half of those frames.",
        why: "Stretching a fast-moving object along its direction of travel is a standard animation technique (squash and stretch) that makes the object look like it has mass and speed. Without it, a fast cursor looks rigid. Stretch is the cursor's long side divided by its short side after all its transforms. The rule reports a cursor when at least 3 of its fast frames, and at least half of them, are stretched less than the threshold.",
        fix: "Scale the cursor along its direction of travel in proportion to its speed, rotated to the angle of travel, and smooth the scale over a few frames. For example, scale it by `1 + min(0.22, speed / 300)`.",
        options: {
            speed: "The speed, in px/s, at or above which a frame counts as fast and the cursor must be stretched.",
            stretch: "The minimum stretch (long side divided by short side) required on a fast frame.",
        },
    },
    check({ film, options, report }) {
        for (const el of cursors(film)) {
            const flat = el.frames.filter((f) => el.step(f) * film.fps >= options.speed &&
                el.at(f).stretch < options.stretch);
            const fast = el.frames.filter((f) => el.step(f) * film.fps >= options.speed);
            if (flat.length >= 3 && flat.length >= 0.5 * fast.length)
                report({
                    element: el,
                    frames: [flat[0], flat[flat.length - 1]],
                    measured: `is stretched less than ${options.stretch} on ${flat.length} of its ${fast.length} frames moving at ${options.speed} px/s or more`,
                    need: `a stretch of at least ${options.stretch} on more than half of the frames where the cursor moves at ${options.speed} px/s or more`,
                });
        }
    },
});
export const deadExit = defineRule({
    id: "cursor/dead-exit",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "A cursor that disappears more than one frame before the film ends has its centre outside the canvas on its last frame, so it leaves past an edge.",
        why: "A cursor that fades out or vanishes in the middle of the canvas ends its motion abruptly, and the viewer doesn't see where it went. Moving it off the edge of the canvas completes the motion, the way a person moves a mouse away.",
        fix: "After the cursor's last click, move it off the nearest edge of the canvas with a short, fast move that eases in, instead of fading it out.",
    },
    check({ film, report }) {
        for (const el of cursors(film)) {
            const last = el.last;
            if (last >= film.frames[film.frames.length - 1] - 1)
                continue;
            const end = el.at(last);
            if (!film.onCanvas(end))
                continue;
            report({
                element: el,
                frames: [Math.max(el.first, last - Math.round(film.fps * 0.3)), last],
                measured: `disappears while still on the canvas, moving at ${(el.step(last) * film.fps).toFixed(0)} px/s on its last frame`,
                need: "the cursor leaves by moving past the edge of the canvas",
            });
        }
    },
});
export const popsIn = defineRule({
    id: "cursor/pops-in",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "A cursor that first appears after the film's first frame has its centre outside the canvas on that frame, so it enters from an edge.",
        why: "A cursor that suddenly appears in the middle of the canvas looks like a jump in the video. Entering from an edge shows it arriving, the way it does when a person moves a mouse onto the screen.",
        fix: "Start the cursor's path outside the canvas, past the edge nearest its first target, and move it in.",
    },
    check({ film, report }) {
        for (const el of cursors(film)) {
            const first = el.at(el.first);
            if (el.first > film.frames[0] && film.onCanvas(first))
                report({
                    element: el,
                    frames: [el.first, el.first],
                    measured: `first appears inside the canvas${first.opacity < 0.5 ? ", fading in" : ""}`,
                    need: "the cursor enters by moving in from past the edge of the canvas",
                });
        }
    },
});
export const harshSettle = defineRule({
    id: "easing/harsh-settle",
    kind: "lint",
    severity: "error",
    options: { warnAbove: 18000, errorAbove: 27000 },
    docs: {
        summary: "An element other than a cursor or text that starts a move on the canvas and stops on the canvas slows down by no more than 18,000 px/s² at 1080p after reaching its top speed (an error above 27,000 px/s²).",
        why: "A card that crosses the canvas in a third of a second has to lose all that speed in a few frames, so it stops abruptly even on an ease-out curve. Viewers judge an object's mass by how long it takes to stop, and only a cursor is light enough to stop that quickly. The rule averages the element's speed over about a fifteenth of a second and measures the largest drop in speed per second after the peak, in px/s² at 1080p. Moves that start off the canvas and elements smaller than 0.2% of the canvas are not checked, because the viewer doesn't see the whole move.",
        fix: "Give the move more time (a move across half the canvas needs about 0.6 to 0.9 s), shorten its path, or use an easing curve that spends longer slowing down, such as a cubic bezier (0.16, 1, 0.3, 1) over more frames.",
        options: {
            warnAbove: "The deceleration, in px/s² at 1080p, above which the rule reports a warning.",
            errorAbove: "The deceleration, in px/s² at 1080p, above which the finding is an error instead of a warning.",
        },
    },
    check({ film, options, report }) {
        const scale = 1080 / film.height;
        // Speeds averaged over about a fifteenth of a second, so a film rendered at 60 fps from
        // 30 fps keys (every other frame a repeat) doesn't measure as braking on every frame.
        const w = Math.max(1, Math.round(film.fps / 15));
        for (const el of movers(film).filter((e) => e.role !== "text" && e.role !== "headline" && e.role !== "label"))
            for (const m of judged(el)) {
                const s0 = el.at(m.from);
                // An entrance from off the canvas can arrive fast: the eye never sees it start. Hard
                // braking only looks abrupt when the whole move happens in view.
                if (!m.lands ||
                    !film.onCanvas(s0) ||
                    Math.max(area(s0.visible), area(el.at(m.to).visible)) <
                        0.002 * film.area)
                    continue;
                const v = m.steps.map((_, i) => {
                    const run = m.steps.slice(Math.max(0, i - w + 1), i + 1);
                    return run.reduce((x, y) => x + y, 0) / run.length;
                });
                const peak = v.indexOf(Math.max(...v));
                let worst = 0;
                for (let i = Math.max(peak, 1); i < v.length; i += 1)
                    worst = Math.max(worst, (v[i - 1] - v[i]) * film.fps * film.fps * scale);
                if (worst <= options.warnAbove)
                    continue;
                report({
                    element: el,
                    frames: [m.from, m.to],
                    measured: `slows down at up to ${Math.round(worst / 1000)}k px/s² at 1080p while stopping a ${m.distance.toFixed(0)} px move that takes ${m.seconds.toFixed(2)} s`,
                    need: `a deceleration of at most ${options.warnAbove / 1000}k px/s² at 1080p`,
                    severity: worst > options.errorAbove ? "error" : "warn",
                });
            }
    },
});
export const cursorContrast = defineRule({
    id: "cursor/low-contrast",
    kind: "lint",
    severity: "error",
    options: { ratio: 1.6, seconds: 0.1 },
    docs: {
        summary: "A cursor never spends 0.1 s or longer over a background whose WCAG contrast ratio with the cursor's fill, outline and drop shadow is below 1.6:1.",
        why: "A cursor the same colour as the bar, button or card under it disappears at the moment it matters most, when it points at something. On each frame where the cursor is at least 50% opaque, the rule takes the topmost background colour under the cursor's tip (or under a point 30% in from its top-left corner when it has no tip) and computes the WCAG contrast ratio with the cursor's fill. An outline or drop shadow that meets the ratio is enough.",
        fix: "Change what's under the cursor (give the hovered element a different colour, or a hover state that lightens it), or point at a different spot. Keep the cursor's own colour, because the viewer recognises the cursor by that colour throughout the film.",
        options: {
            ratio: "The minimum WCAG contrast ratio between the cursor and the background under it (1 means identical colours, 21 is black on white).",
            seconds: "How long, in seconds, the contrast must stay below the ratio before the rule reports it.",
        },
    },
    check({ film, options, report }) {
        for (const el of film.elements.filter((e) => e.role === "cursor")) {
            const blend = [];
            let what = "";
            for (const f of el.frames) {
                const s = el.at(f);
                const glyph = [s.raw, ...el.contents(f)].find((r) => r.pf && parseColour(r.pf));
                const fill = parseColour(glyph?.pf);
                if (!fill || s.opacity < 0.5)
                    continue;
                const p = s.raw.tp
                    ? { x: s.raw.tp[0], y: s.raw.tp[1] }
                    : { x: s.box[0] + s.box[2] * 0.3, y: s.box[1] + s.box[3] * 0.3 };
                // The topmost painted surface under the pointer, outside the cursor itself.
                const under = film
                    .rows(f)
                    .filter((r) => r.t !== "t" &&
                    r.bg &&
                    !isAncestor(s.raw.k, r.k) &&
                    r.k !== s.raw.k &&
                    r.o > 0.5)
                    .filter((r) => p.x >= r.v[0] &&
                    p.x <= r.v[0] + r.v[2] &&
                    p.y >= r.v[1] &&
                    p.y <= r.v[1] + r.v[3])
                    .map((r) => ({ r, c: parseColour(r.bg) }))
                    .filter((x) => x.c && x.c[3] > 0.5)
                    .pop();
                // An outline or halo (a stroke, or a drop-shadow around the wedge) that stands out from
                // the surface separates the cursor even when its fill matches.
                const edges = [
                    glyph?.ps,
                    ...(glyph?.sh ?? "")
                        .split(" | ")
                        .filter((x) => x.includes("drop-shadow"))
                        .map((x) => x.match(/rgba?\([^)]*\)/)?.[0]),
                ]
                    .map((c) => parseColour(c ?? undefined))
                    .filter((c) => !!c && c[3] > 0.5);
                const separated = under && edges.some((e) => contrast(e, under.c) >= options.ratio);
                if (under && !separated && contrast(fill, under.c) < options.ratio) {
                    blend.push(f);
                    what = `${under.r.bg} under a ${glyph.pf} cursor`;
                }
            }
            for (const [a, b] of runs(blend, 2))
                if ((b - a + 1) / film.fps >= options.seconds)
                    report({
                        element: el,
                        frames: [a, b],
                        measured: `has too little contrast with the background under it (${what})`,
                        need: `a contrast ratio of at least ${options.ratio}:1 between the cursor and the background under it`,
                    });
        }
    },
});
export const motionRules = [
    linear,
    weakOut,
    noEaseIn,
    abruptStop,
    stutter,
    edgeReversal,
    cursorStretch,
    deadExit,
    popsIn,
    cursorContrast,
    harshSettle,
];
