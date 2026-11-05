/**
 * The compiler: what's broken in any video, whoever made it and whatever its taste.
 * Always on, no options.
 */
import { area, centreOf, intersect, isAncestor, readText } from "../film.js";
import { defineRule } from "../rule.js";
import { carried, contains, frames as framesIn, px, runs } from "./util.js";
export const jump = defineRule({
    id: "continuity/jump",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "No element moves or changes size by more than 40 px at 1080p in a single frame when that change is also more than 4 times the largest change 2 to 3 frames before or after it (or 4 px at 1080p, if that is larger).",
        why: "A viewer sees a sudden jump in position or size as a glitch. It is almost always caused by two keyframes or two layouts that don't meet: a path that resumes from a different point than where it stopped, or a component replaced by another at a new position. For each frame, the rule takes the larger of how far the element's centre moved and how much the diagonal of its box changed (for a cursor, only how far it moved, because a cursor shrinking as it clicks is intended). Frames where the element is below 30% opacity, or where its text changed (typing, a counter), are skipped, and a jump spread over two neighbouring frames, as when a 30 fps animation is rendered at 60 fps, counts as one jump.",
        fix: "Go to the frames in the finding and make the motion before and after them start and end at the same position and size. If one element replaces another, start the new one exactly where the old one ends.",
    },
    check({ film, report }) {
        for (const el of film.elements) {
            const fs = el.frames;
            const change = fs.map((f, i) => {
                const prev = el.at(fs[i - 1]);
                const cur = el.at(f);
                // Text that changes (typing, a counter) changes size without moving.
                if (!prev ||
                    fs[i - 1] !== f - 1 ||
                    prev.opacity < 0.3 ||
                    cur.opacity < 0.3 ||
                    prev.raw.x !== cur.raw.x)
                    return 0;
                const size = (s) => Math.hypot(s.box[2], s.box[3]);
                // A cursor's press squash changes its size in a frame by design; its position is what jumps.
                return el.role === "cursor"
                    ? el.step(f)
                    : Math.max(el.step(f), Math.abs(size(cur) - size(prev)));
            });
            change.forEach((c, i) => {
                // Compared with motion two or three frames away, so a jump rendered across two frames (60 fps
                // from 30 fps keys) still stands out; its other half may be the frame right beside it.
                const around = Math.max(change[i - 2] ?? 0, change[i + 2] ?? 0, change[i - 3] ?? 0, change[i + 3] ?? 0, px(film, 4));
                const pair = Math.max(change[i - 1] ?? 0, change[i + 1] ?? 0);
                const isJump = c > px(film, 40) &&
                    c > 4 * around &&
                    (pair < px(film, 40) || pair > 0.5 * c);
                const first = isJump && !(pair > 0.5 * c && (change[i - 1] ?? 0) > px(film, 40));
                // A child carried by a jumping parent is the parent's jump, reported once.
                if (first && !carried(film, fs[i], el.at(fs[i]).raw.k)) {
                    report({
                        element: el,
                        frames: [fs[i - 1], fs[i]],
                        measured: `moves or changes size by ${c.toFixed(0)} px in one frame, while the largest change 2 to 3 frames before or after is ${around.toFixed(0)} px`,
                        need: "a continuous path, with no single-frame change over 40 px at 1080p that is more than 4 times the change in the frames around it",
                    });
                }
            });
        }
    },
});
export const flicker = defineRule({
    id: "continuity/flicker",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "No element is visible for 0.07 s or less (2 frames at 30 fps) and never again.",
        why: "An element that is on screen for only a frame or two is too brief to read but long enough to notice, so it looks like a glitch. The rule counts an element as visible in a frame when it is above 30% opacity and its visible part covers more than 0.3% of the canvas. It reports an element whose visible frames form a single unbroken run of 0.07 s or less, and that is present in the film, visible or not, for at most 2 frames longer than that.",
        fix: "Check the conditions that show the element at that time. Usually the end of a frame range is off by one, or an element's exit and the next element's entrance overlap by a frame.",
    },
    check({ film, report }) {
        const longest = framesIn(film, 0.07);
        for (const el of film.elements) {
            const seen = el.frames.filter((f) => {
                const s = el.at(f);
                return s.opacity > 0.3 && area(s.visible) > 0.003 * film.area;
            });
            if (!seen.length)
                continue;
            const spans = runs(seen, 1);
            if (spans.length === 1 &&
                spans[0][1] - spans[0][0] + 1 <= longest &&
                el.frames.length <= longest + 2) {
                report({
                    element: el,
                    frames: spans[0],
                    measured: `is visible for ${spans[0][1] - spans[0][0] + 1} frame(s) and at no other time`,
                    need: "the element is visible for longer than 0.07 s, or not shown at all",
                });
            }
        }
    },
});
export const textCut = defineRule({
    id: "layout/text-cut",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "Text that stays still for 0.5 s or longer has at least 85% of its area visible, not cut off by its own box, a container that clips it, or the edge of the canvas.",
        why: "Words that are cut off look like a layout bug, and on a phone they can't be read at all. The rule checks every text and headline in each frame where it is above 50% opacity and moves less than 0.5 px from the previous frame. It compares the area of the text's glyphs that is visible with the full area of the glyphs, and reports a run of such frames lasting 0.5 s or longer in which less than 85%, but more than none, is visible. Text that is cut off only while it moves, such as text scrolling past, is not reported.",
        fix: "Give the container room for its text (make it wider or taller, or the type smaller), move the text inside the visible area, or pan so the whole text is visible before it stops moving.",
    },
    check({ film, report }) {
        const hold = framesIn(film, 0.5);
        for (const el of film.elements) {
            if (el.role !== "text" && el.role !== "headline")
                continue;
            const cut = el.frames.filter((f) => {
                const s = el.at(f);
                return (s.opacity > 0.5 &&
                    area(s.box) > 0 &&
                    area(s.visible) < 0.85 * area(s.box) &&
                    area(s.visible) > 0 &&
                    el.step(f) < 0.5);
            });
            for (const [a, b] of runs(cut, 1)) {
                if (b - a + 1 < hold)
                    continue;
                const s = el.at(a);
                report({
                    element: el,
                    frames: [a, b],
                    measured: `has ${Math.round((1 - area(s.visible) / area(s.box)) * 100)}% of the text "${(s.raw.x ?? "").slice(0, 30)}" cut off for ${(b - a + 1) / film.fps} s while it stays still`,
                    need: "at least 85% of the text visible whenever it stays still for 0.5 s or longer",
                });
            }
        }
    },
});
export const missingImage = defineRule({
    id: "assets/missing-image",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "Every image in the film loads when the frames are rendered.",
        why: "An image that fails to load is drawn as nothing, or as the browser's broken-image icon, so the viewer sees an empty space or an obvious error. The rule reports every element whose image the browser marked as failed to load in any frame.",
        fix: "Check the image's path. Reference files in `public/` with `staticFile()`, and make sure the file exists when the film is rendered.",
    },
    check({ film, report }) {
        for (const el of film.elements) {
            const broken = el.frames.filter((f) => el.at(f).raw.br);
            if (broken.length)
                report({
                    element: el,
                    frames: [broken[0], broken[broken.length - 1]],
                    measured: "is an image that failed to load, so nothing or a broken-image icon is drawn in its place",
                    need: "the image file loads when the frames are rendered",
                });
        }
    },
});
export const fallbackFont = defineRule({
    id: "assets/fallback-font",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "All text is drawn in the font its CSS declares, never in a fallback font because the declared font didn't load.",
        why: "A fallback font changes the width of every line and the look of the brand, so a layout designed for the real font breaks. The rule reports each declared font family that the browser replaced with a fallback in any frame, with the first and last frame where that happened.",
        fix: "Load the font before the frames render: call `loadFont()` from `@remotion/google-fonts`, or load a `FontFace` and pause rendering with `delayRender` until it has loaded.",
    },
    check({ film, report }) {
        const seen = new Map();
        for (const f of film.frames)
            for (const r of film.rows(f))
                if (r.fb)
                    seen.set(r.fb, [...(seen.get(r.fb) ?? []), f]);
        for (const [family, fs] of seen) {
            report({
                frames: [fs[0], fs[fs.length - 1]],
                measured: `The font "${family}" is declared but didn't load, so its text was drawn in a fallback font`,
                need: "the declared font is loaded before the frames render",
            });
        }
    },
});
export const offCanvas = defineRule({
    id: "layout/subject-off-canvas",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "A tagged element (other than a cursor) that is shown above 50% opacity for at least 0.5 s has its centre on the canvas in at least one of those frames.",
        why: "A tagged element is one the film is about, so if it is never on screen the viewer sees nothing where something was meant to be. Being off the canvas for part of the time, such as before an entrance or after an exit, is fine; the rule only reports an element whose centre is outside the canvas in every frame where it is above 50% opacity.",
        fix: "Check the element's position. A coordinate is probably in the wrong space (canvas coordinates used inside a scaled or moved parent), or the animation that brings it onto the canvas never runs.",
    },
    check({ film, report }) {
        for (const el of film.elements) {
            if (!el.tagged || el.role === "cursor")
                continue;
            const shown = el.frames.filter((f) => el.at(f).opacity > 0.5);
            if (shown.length < framesIn(film, 0.5) ||
                shown.some((f) => film.onCanvas(el.at(f))))
                continue;
            const c = centreOf(el.at(shown[0]));
            report({
                element: el,
                frames: [shown[0], shown[shown.length - 1]],
                measured: `has its centre outside the canvas in every frame where it is shown (in the first, its centre is at (${c.x.toFixed(0)}, ${c.y.toFixed(0)}) px)`,
                need: "the element's centre is on the canvas in at least one frame while it is shown",
            });
        }
    },
});
/** A painted background you can't see through. */
const opaque = (bg) => {
    if (!bg)
        return false;
    const alpha = bg.match(/rgba\([^)]*,\s*([\d.]+)\)/) ?? bg.match(/\/\s*([\d.]+)\s*\)/);
    return !alpha || Number(alpha[1]) >= 0.9;
};
export const textOverlap = defineRule({
    id: "layout/text-overlap",
    kind: "check",
    severity: "error",
    options: {},
    docs: {
        summary: "Two different texts above 20% opacity never overlap by 30% or more of the smaller one's area for 0.1 s or longer (and at least 3 frames), unless an opaque background behind the upper text covers the overlap.",
        why: "Overlapping words can't be read, even if they overlap only briefly, and the overlap looks like a glitch. It happens most often when one message replaces another: the old one fades out in the same place the new one fades in, a line slides under another, or labels are stacked on each other. Words or letters set as separate pieces side by side on one line count as one text, and identical texts or texts nested inside each other are not compared.",
        fix: "Let the old text leave before the new one appears, move one out of the other's way (for example, push the old line up and off), or put the new text on an opaque background that covers the old one.",
    },
    check({ film, report }) {
        const hits = new Map();
        for (const f of film.frames) {
            const rows = film.rows(f);
            const texts = rows
                .map((r, i) => ({ r, i }))
                .filter(({ r }) => r.t === "t" &&
                /[A-Za-z0-9]/.test(r.x ?? "") &&
                r.o > 0.2 &&
                area(r.v) > 0)
                .sort((p, q) => p.r.v[0] - q.r.v[0]);
            // Spans set side by side on one baseline under one parent are one line of text (a word or
            // letter at a time), so a finding names the line, not each span.
            const lines = [];
            const lineOf = new Map();
            const parent = (k) => k.slice(0, k.lastIndexOf("/"));
            for (const t of texts) {
                const line = lines.find((l) => l.every((x) => parent(x.r.k) === parent(t.r.k) &&
                    Math.abs(x.r.v[1] + x.r.v[3] / 2 - (t.r.v[1] + t.r.v[3] / 2)) <
                        0.5 * Math.max(x.r.v[3], t.r.v[3]) &&
                    area(intersect(x.r.v, t.r.v)) <
                        0.3 * Math.min(area(x.r.v), area(t.r.v))) &&
                    t.r.v[0] - Math.max(...l.map((x) => x.r.v[0] + x.r.v[2])) <
                        1.2 * (t.r.fs ?? 20));
                if (line)
                    line.push(t);
                else
                    lines.push([t]);
                lineOf.set(t.i, line ?? lines[lines.length - 1]);
            }
            const covers = rows
                .map((r, i) => ({ r, i }))
                .filter(({ r }) => r.t === "b" && r.o > 0.9 && opaque(r.bg));
            for (let i = 0; i < texts.length; i += 1)
                for (let j = i + 1; j < texts.length &&
                    texts[j].r.v[0] < texts[i].r.v[0] + texts[i].r.v[2]; j += 1) {
                    const [p, q] = [texts[i], texts[j]];
                    if ((p.r.x ?? "").trim() === (q.r.x ?? "").trim() ||
                        isAncestor(p.r.k, q.r.k) ||
                        isAncestor(q.r.k, p.r.k))
                        continue;
                    const shared = intersect(p.r.v, q.r.v);
                    if (area(shared) < 0.3 * Math.min(area(p.r.v), area(q.r.v)))
                        continue;
                    // Hidden, not overlapping: an opaque box painted after the lower text covers the overlap.
                    const [lower, upper] = p.i < q.i ? [p, q] : [q, p];
                    if (covers.some((c) => c.i > lower.i &&
                        isAncestor(c.r.k, upper.r.k) &&
                        !isAncestor(c.r.k, lower.r.k) &&
                        contains(c.r.v, shared)))
                        continue;
                    const [la, lb] = [lineOf.get(lower.i), lineOf.get(upper.i)];
                    if (la === lb)
                        continue;
                    const [a, b] = [la, lb].map((l) => readText(l.map((x) => x.r)));
                    const id = [a, b].sort().join("\u0000");
                    const h = hits.get(id) ?? { frames: [], key: lower.r.k, a, b };
                    if (h.frames[h.frames.length - 1] !== f)
                        h.frames.push(f);
                    hits.set(id, h);
                }
        }
        for (const h of hits.values())
            for (const [a, b] of runs(h.frames, 1)) {
                if (b - a + 1 < Math.max(3, framesIn(film, 0.1)))
                    continue;
                report({
                    element: film.elementOf(a, h.key),
                    frames: [a, b],
                    measured: `"${h.a.slice(0, 30)}" and "${h.b.slice(0, 30)}" are drawn on top of each other, overlapping by at least 30% of the smaller one's area, for ${((b - a + 1) / film.fps).toFixed(2)} s`,
                    need: "only one text at a time in any spot: no two different texts overlap by 30% or more of the smaller one's area for 0.1 s or longer",
                });
            }
    },
});
export const compilerRules = [
    jump,
    flicker,
    textCut,
    textOverlap,
    missingImage,
    fallbackFont,
    offCanvas,
];
