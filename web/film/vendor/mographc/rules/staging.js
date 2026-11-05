/**
 * Layout, pacing and animation: whether the viewer can read what's shown in time, and whether
 * everything that changes on screen is animated rather than switched in a single frame.
 */
import { area, intersect, intersectArea, isAncestor, } from "../film.js";
import { defineRule } from "../rule.js";
import { frames as framesIn, px, runs, surfacesAt } from "./util.js";
export const titleOffCentre = defineRule({
    id: "staging/title-off-centre",
    kind: "lint",
    severity: "error",
    options: { tolerance: 16, vertical: 64 },
    docs: {
        summary: "When the canvas shows only text and graphics (no surface or button), the centre of the group they form is within 16 px at 1080p of the canvas centre horizontally and within 64 px at 1080p vertically.",
        why: "With nothing else on screen, viewers judge the position of a title against the edges of the canvas, so a title a few dozen pixels off centre looks like a mistake. The rule checks frames where no surface (a window, card or panel) covering more than 0.2% of the canvas and no button is above 30% opacity. It takes the bounding box of every other element above 30% opacity that covers less than half the canvas, skips frames with fewer than two such elements or a group wider than 80% of the canvas, and reports an offset that lasts at least 0.33 s. The vertical tolerance is larger because a title with a tagline below it is often placed slightly above centre on purpose.",
        fix: "Move the group by the offset the finding gives. Centre the visible letters and graphics rather than their layout box, because an icon, a gap or letter spacing inside the box can shift where the content actually sits.",
        options: {
            tolerance: "How far, in px at 1080p, the group's centre may be from the canvas centre horizontally.",
            vertical: "How far, in px at 1080p, the group's centre may be from the canvas centre vertically.",
        },
    },
    check({ film, options, report }) {
        const tol = px(film, options.tolerance);
        const bad = [];
        for (const f of film.frames) {
            // Any surface on screen, even a thin row about to grow, means the frame isn't showing only a title.
            if (surfacesAt(film, f, 0.002).length ||
                film.find({ role: "button" }).some((b) => (b.at(f)?.opacity ?? 0) > 0.3))
                continue;
            const items = film.elements
                .filter((e) => !["cursor", "part", "label", "surface", "button"].includes(e.role))
                .map((e) => e.at(f))
                .filter((s) => s &&
                s.opacity > 0.3 &&
                area(s.visible) > 0 &&
                area(s.visible) < 0.5 * film.area);
            if (items.length < 2)
                continue;
            const x0 = Math.min(...items.map((s) => s.visible[0]));
            const x1 = Math.max(...items.map((s) => s.visible[0] + s.visible[2]));
            const y0 = Math.min(...items.map((s) => s.visible[1]));
            const y1 = Math.max(...items.map((s) => s.visible[1] + s.visible[3]));
            const dx = (x0 + x1) / 2 - film.width / 2;
            const dy = (y0 + y1) / 2 - film.height / 2;
            // Vertically, a title with a tagline below it is often placed slightly high on purpose; horizontally it never is.
            if ((Math.abs(dx) > tol || Math.abs(dy) > px(film, options.vertical)) &&
                x1 - x0 < 0.8 * film.width)
                bad.push([f, dx, dy]);
        }
        for (const [a, b] of runs(bad.map((x) => x[0]), 1)) {
            if (b - a + 1 < framesIn(film, 0.33))
                continue;
            const mid = bad.filter((x) => x[0] >= a && x[0] <= b);
            const med = (i) => mid.map((x) => x[i]).sort((p, q) => p - q)[Math.floor(mid.length / 2)];
            report({
                frames: [a, b],
                measured: `the group of text and graphics on screen has its centre (${med(1) > 0 ? "+" : ""}${med(1).toFixed(0)}, ${med(2) > 0 ? "+" : ""}${med(2).toFixed(0)}) px from the canvas centre (horizontal, vertical; positive is right and down)`,
                need: `the group's centre within ${options.tolerance} px at 1080p of the canvas centre horizontally and within ${options.vertical} px at 1080p vertically`,
            });
        }
    },
});
export const noPadding = defineRule({
    id: "staging/no-padding",
    kind: "lint",
    severity: "error",
    options: { inset: 14 },
    docs: {
        summary: "When a surface (a window, card or panel) covering 12% to 90% of the canvas is standing still, the text inside it is at least 14 px at 1080p from its visible edges, and at most one line of that text is cut off by them.",
        why: "A window whose content touches or runs past its edges looks broken, even if the real product does that at its own size. The rule looks at text smaller than 40 px at 1080p and above 50% opacity inside a surface above 90% opacity, and measures the smallest distance from that text to the surface's visible edges; a line counts as cut off when less than 90% of it is visible. It only checks frames where the surface and the text have each moved 1 px or less since the previous frame, because a page scrolling past an edge is motion rather than a layout problem, and it reports problems that last at least 0.5 s.",
        fix: "Add padding between the surface's edge and its content, or move or zoom the view (pan, or zoom in slightly less) so the content sits inside the surface with room around it.",
        options: {
            inset: "The smallest distance, in px at 1080p, allowed between text and the visible edge of the surface it is inside.",
        },
    },
    check({ film, options, report }) {
        const inset = px(film, options.inset);
        for (const el of film.elements.filter((e) => e.role === "surface")) {
            const hits = [];
            for (const f of el.frames) {
                const s = el.at(f);
                const prev = el.at(f - 1);
                const v = s.visible;
                if (s.opacity < 0.9 ||
                    area(v) < 0.12 * film.area ||
                    area(v) > 0.9 * film.area)
                    continue;
                if (!prev ||
                    prev.visible.some((x, i) => Math.abs(x - v[i]) > 1) ||
                    prev.box.some((x, i) => Math.abs(x - s.box[i]) > 1))
                    continue;
                let gap = Infinity;
                let cut = 0;
                for (const r of film.rows(f)) {
                    if (r.t !== "t" ||
                        r.o < 0.5 ||
                        (r.fs ?? 0) >= px(film, 40) ||
                        !isAncestor(s.raw.k, r.k))
                        continue;
                    if (area(r.v) <= 0)
                        continue;
                    const before = film.rows(f - 1).find((p) => p.k === r.k);
                    if (!before || before.b.some((x, i) => Math.abs(x - r.b[i]) > 1))
                        continue;
                    if (area(r.v) < 0.9 * area(r.b))
                        cut += 1;
                    gap = Math.min(gap, r.v[0] - v[0], r.v[1] - v[1], v[0] + v[2] - r.v[0] - r.v[2], v[1] + v[3] - r.v[1] - r.v[3]);
                }
                if (gap < inset || cut >= 2)
                    hits.push([f, gap, cut]);
            }
            for (const [a, b] of runs(hits.map((h) => h[0]), 2)) {
                if (b - a + 1 < framesIn(film, 0.5))
                    continue;
                const mid = hits.filter((h) => h[0] >= a && h[0] <= b);
                const gap = mid.map((h) => h[1]).sort((p, q) => p - q)[Math.floor(mid.length / 2)];
                const cut = Math.max(...mid.map((h) => h[2]));
                report({
                    element: el,
                    frames: [a, b],
                    measured: `has text ${Math.max(0, gap).toFixed(0)} px from its edge${cut ? `, and ${cut} lines of text cut off by its edge` : ""}`,
                    need: `text at least ${options.inset} px at 1080p from the surface's edges, and at most one line cut off`,
                });
            }
        }
    },
});
export const textTooSmall = defineRule({
    id: "staging/text-too-small",
    kind: "lint",
    severity: "error",
    options: { minSize: 14, briefSeconds: 1.5 },
    docs: {
        summary: "While a surface (a window, card or panel) covers 25% to 95% of the canvas, the median font size of the text inside it is at least 14 px at 1080p; stretches shorter than 1 s are ignored.",
        why: "Product videos are often watched on phones, where smaller text can't be read. A whole page shrunk to fit the canvas shows that the page exists, but nobody can read it. The rule takes the text inside surfaces above 90% opacity that is above 50% opacity and smaller than 40 px at 1080p, and only checks frames with at least 12 such pieces of text. The first stretch of small text is reported as a warning instead of an error if it lasts 1.5 s or less, so one short full view of a page to show its size is allowed.",
        fix: "Zoom in on the part of the surface that matters and pan or scroll through it, instead of shrinking the whole surface to fit the canvas.",
        options: {
            minSize: "The smallest median font size, in px at 1080p, allowed for the text inside a large surface.",
            briefSeconds: "The longest time, in seconds, the first stretch of small text may last and still be reported only as a warning.",
        },
    },
    check({ film, options, report }) {
        const small = [];
        for (const f of film.frames) {
            const surfaces = surfacesAt(film, f, 0.25, 0.95).filter((s) => s.opacity > 0.9);
            if (!surfaces.length)
                continue;
            const sizes = film
                .rows(f)
                .filter((r) => r.t === "t" &&
                (r.fs ?? 0) > 0 &&
                (r.fs ?? 0) < px(film, 40) &&
                r.o > 0.5 &&
                surfaces.some((s) => isAncestor(s.raw.k, r.k)))
                .map((r) => r.fs)
                .sort((a, b) => a - b);
            if (sizes.length >= 12 &&
                sizes[Math.floor(sizes.length / 2)] < px(film, options.minSize))
                small.push([f, sizes[Math.floor(sizes.length / 2)]]);
        }
        let first = true;
        for (const [a, b] of runs(small.map((s) => s[0]), 2)) {
            const seconds = (b - a + 1) / film.fps;
            if (seconds < 1)
                continue;
            const brief = first && seconds <= options.briefSeconds;
            first = false;
            const med = small
                .filter((s) => s[0] >= a && s[0] <= b)
                .map((s) => s[1])
                .sort((p, q) => p - q);
            report({
                frames: [a, b],
                measured: `a surface is on screen for ${seconds.toFixed(1)} s with a median font size of ${med[Math.floor(med.length / 2)].toFixed(0)} px for its text${brief ? " (one short view of a whole page is allowed, so this is only a warning)" : ""}`,
                need: `a median font size of at least ${options.minSize} px at 1080p`,
                severity: brief ? "warn" : "error",
            });
        }
    },
});
/** Headline text on screen in a frame: large text that isn't inside a surface. */
const headlineLetters = (film, f) => film.find({ role: "headline" }).filter((e) => {
    const s = e.at(f);
    return s && s.opacity > 0.05 && s.raw.x;
});
export const textOverProduct = defineRule({
    id: "staging/text-over-product",
    kind: "lint",
    severity: "error",
    options: { seconds: 0.2 },
    docs: {
        summary: "A headline never lies partly over a surface (a window, card or panel) for 0.2 s or longer, where partly means that 5% to 85% of the headline's area is over the surface.",
        why: "Text that cuts across an interface looks like a layout mistake and makes both the words and the product harder to read. The rule compares each headline with every surface above 25% opacity that covers 2% to 90% of the canvas. A headline inside the surface, and a box drawn by the headline's own component (such as a highlight behind its words), don't count.",
        fix: "Give the headline its own area of the canvas, or move the surface off screen before the headline appears (or the headline before the surface appears).",
        options: {
            seconds: "The shortest overlap, in seconds, that is reported.",
        },
    },
    check({ film, options, report }) {
        const hits = new Map();
        for (const f of film.frames) {
            const surfaces = film.elements.filter((e) => {
                const s = e.at(f);
                return (e.role === "surface" &&
                    s &&
                    s.opacity > 0.25 &&
                    area(s.visible) > 0.02 * film.area &&
                    area(s.visible) < 0.9 * film.area);
            });
            for (const h of headlineLetters(film, f)) {
                const hs = h.at(f);
                for (const sEl of surfaces) {
                    const ss = sEl.at(f);
                    // A backdrop drawn by the headline's own component (a highlight behind its words) is part of the headline.
                    const root = ss.raw.k.split("/").slice(0, -2).join("/");
                    const container = film.rows(f).find((r) => r.k === root);
                    const own = isAncestor(root, hs.raw.k) &&
                        (!container
                            ? root.split("/").length > 2
                            : area(container.v) < 0.5 * film.area);
                    if (isAncestor(ss.raw.k, hs.raw.k) || own)
                        continue;
                    const ov = intersectArea(hs.box, ss.visible);
                    if (ov > 0.05 * area(hs.box) && ov < 0.85 * area(hs.box)) {
                        const key = `${h.id}|${sEl.id}`;
                        const hit = hits.get(key) ?? { el: h, other: sEl, frames: [] };
                        hit.frames.push(f);
                        hits.set(key, hit);
                    }
                }
            }
        }
        for (const { el, other, frames } of hits.values())
            for (const [a, b] of runs(frames, 2)) {
                if ((b - a + 1) / film.fps < options.seconds)
                    continue;
                report({
                    element: el,
                    frames: [a, b],
                    measured: `its text "${(el.at(a).raw.x ?? "").slice(0, 24)}" partly overlaps ${other.label} for ${b - a + 1} frames`,
                    need: "the headline never partly over a surface",
                });
            }
    },
});
export const doubleExposure = defineRule({
    id: "staging/double-exposure",
    kind: "lint",
    severity: "warn",
    options: {},
    docs: {
        summary: "When one surface (a window, card or panel) fades in or out over another, the two are not both above 35% opacity for 3 frames or more.",
        why: "When two large surfaces are both clearly visible while one fades over the other, the viewer sees two things at once and can take in neither. The rule looks at surfaces that cover 6% to 95% of the canvas, aren't inside one another, and overlap by at least 30% of the smaller one's area. It counts frames where both are above 35% opacity, at least one is below 98%, and at least one changed opacity by more than 0.02 since the previous frame.",
        fix: "Fade or move the outgoing surface out faster, or start the incoming one later, so one is mostly gone before the other is mostly visible. Alternatively, animate one surface changing into the next instead of crossfading the two.",
    },
    check({ film, report }) {
        const hits = [];
        for (const f of film.frames) {
            const big = film.elements.filter((e) => {
                const s = e.at(f);
                return (e.role === "surface" &&
                    s &&
                    s.opacity > 0.35 &&
                    area(s.visible) > 0.06 * film.area &&
                    area(s.visible) < 0.95 * film.area);
            });
            const found = big.some((a, i) => big.slice(i + 1).some((b) => {
                const sa = a.at(f);
                const sb = b.at(f);
                if (isAncestor(sa.raw.k, sb.raw.k) || isAncestor(sb.raw.k, sa.raw.k))
                    return false;
                if (intersectArea(sa.visible, sb.visible) <
                    0.3 * Math.min(area(sa.visible), area(sb.visible)))
                    return false;
                const fading = [a, b].some((e) => {
                    const p = e.at(f - 1);
                    return p && Math.abs(p.opacity - e.at(f).opacity) > 0.02;
                });
                return fading && Math.min(sa.opacity, sb.opacity) < 0.98;
            }));
            if (found)
                hits.push(f);
        }
        for (const [a, b] of runs(hits))
            if (b - a >= 2)
                report({
                    frames: [a, b],
                    measured: `two overlapping surfaces are both above 35% opacity while one of them fades, for ${b - a + 1} frames`,
                    need: "only one of the overlapping surfaces above 35% opacity at a time",
                });
    },
});
export const topBanner = defineRule({
    id: "staging/top-banner",
    kind: "lint",
    severity: "warn",
    options: {},
    docs: {
        summary: "No headline starts in the top 19% of the canvas height while also being wider than 47% of the canvas width.",
        why: "A wide, single-line headline across the top of the canvas, with the product below it, looks like a subtitle added afterwards rather than part of the design. The rule uses each headline's box in the middle of the time it is fully visible.",
        fix: "Place the headline in its own area beside or around the main element on screen, or break it onto more lines so it is narrower than 47% of the canvas.",
    },
    check({ film, report }) {
        for (const h of film.headlines())
            if (h.box[1] < 0.19 * film.height && h.box[2] > 0.47 * film.width)
                report({
                    frames: [h.from, h.to],
                    measured: `the headline "${h.text.slice(0, 30)}" starts in the top 19% of the canvas and is wider than 47% of it`,
                    need: "the headline lower on the canvas, or narrower than 47% of its width",
                });
    },
});
export const textHold = defineRule({
    id: "pace/text-hold",
    kind: "lint",
    severity: "warn",
    options: { wordsPerSecond: 3.5, minimum: 0.5 },
    docs: {
        summary: "Each headline stays fully visible for at least 1 second per 3.5 words, and never less than 0.5 s.",
        why: "Viewers need time to read a line, and the time it spends animating in doesn't count because its words aren't all there yet. A line that leaves as soon as it is fully visible is noticed but not read. The rule measures the time from the first to the last frame in which every letter of the headline is on screen above 95% opacity, and counts words by splitting the text at spaces.",
        fix: "Keep the line fully visible for longer before it leaves, or use fewer words.",
        options: {
            wordsPerSecond: "The assumed reading speed, in words per second. A headline needs its word count divided by this number of seconds.",
            minimum: "The shortest time, in seconds, any headline must stay fully visible, however few words it has.",
        },
    },
    check({ film, options, report }) {
        for (const h of film.headlines()) {
            if (!h.full)
                continue;
            const held = (h.full[1] - h.full[0]) / film.fps;
            const words = Math.max(1, h.text.split(/\s+/).length);
            const need = Math.max(options.minimum, words / options.wordsPerSecond);
            if (held < need)
                report({
                    frames: h.full,
                    measured: `the headline "${h.text.slice(0, 30)}" is fully visible for ${held.toFixed(2)} s`,
                    need: `at least ${need.toFixed(2)} s fully visible for its ${words} words`,
                });
        }
    },
});
export const neverFinishes = defineRule({
    id: "pace/text-never-finishes",
    kind: "lint",
    severity: "error",
    options: {},
    docs: {
        summary: "Every headline is fully visible, with all its letters on screen above 95% opacity, in at least one frame before it leaves.",
        why: "A line that starts leaving before its last word has appeared is never seen whole, so it can't be read. Headlines on screen for fewer than 7 frames are not checked.",
        fix: "Start the headline's exit only after its last word has fully appeared, and keep it fully visible long enough to read (see pace/text-hold).",
    },
    check({ film, report }) {
        for (const h of film.headlines())
            if (!h.full)
                report({
                    frames: [h.from, h.to],
                    measured: `the headline "${h.text.slice(0, 30)}" is never fully visible: in no frame are all its letters on screen above 95% opacity`,
                    need: "every letter of the headline on screen above 95% opacity in at least one frame before it leaves",
                });
    },
});
export const fadeOnly = defineRule({
    id: "animate/fade-only",
    kind: "lint",
    severity: "warn",
    options: {},
    docs: {
        summary: "A surface (a window, card or panel) that fades in or out also moves at least 6 px or changes its width by at least 4% during the fade, rather than only changing opacity.",
        why: "A large surface that only fades while standing still looks unfinished next to elements that move. The rule finds each fade in which a surface's opacity rises from 15% or less to 85% or more on consecutive frames (or falls the other way), and compares the surface's position and width at the start and end of the fade. It ignores surfaces that cover 3% of the canvas or less, and when surfaces inside one another fade together it reports only the outermost one.",
        fix: "Add a small move (6 px or more) or scale (4% or more) to the entrance or exit, with the same timing and easing as the fade.",
    },
    check({ film, report }) {
        const found = [];
        for (const el of film.elements.filter((e) => e.role === "surface")) {
            for (const [kind, from, to] of opacitySpans(el)) {
                const a = el.at(from);
                const b = el.at(to);
                const moved = Math.hypot(a.box[0] - b.box[0], a.box[1] - b.box[1]);
                const scaled = Math.abs(b.box[2] / Math.max(a.box[2], 1) - 1);
                const s = kind === "appear" ? b : a;
                if (moved < 6 &&
                    scaled < 0.04 &&
                    area(s.visible) > 0.03 * film.area &&
                    el.frames.length > 2)
                    found.push({ el, kind, from, to, key: s.raw.k });
            }
        }
        // A surface fading with its contents is one fade.
        for (const x of found)
            if (!found.some((o) => o !== x &&
                isAncestor(o.key, x.key) &&
                o.from <= x.to &&
                o.to >= x.from))
                report({
                    element: x.el,
                    frames: [x.from, x.to],
                    measured: `${x.kind === "appear" ? "fades in" : "fades out"} over ${x.to - x.from} frames without moving or scaling`,
                    need: "a move of at least 6 px or a change in width of at least 4% during the fade",
                });
    },
});
const opacitySpans = (el) => {
    const fs = el.frames;
    const out = [];
    let i = 0;
    while (i < fs.length - 1) {
        let j = i;
        const op = (k) => el.at(fs[k]).opacity;
        if (op(i + 1) > op(i) + 0.01) {
            while (j + 1 < fs.length &&
                op(j + 1) >= op(j) - 0.005 &&
                fs[j + 1] === fs[j] + 1)
                j += 1;
            if (op(i) <= 0.15 && op(j) >= 0.85)
                out.push(["appear", fs[i], fs[j]]);
        }
        else if (op(i + 1) < op(i) - 0.01) {
            while (j + 1 < fs.length &&
                op(j + 1) <= op(j) + 0.005 &&
                fs[j + 1] === fs[j] + 1)
                j += 1;
            if (op(i) >= 0.85 && op(j) <= 0.15)
                out.push(["leave", fs[i], fs[j]]);
        }
        i = Math.max(j, i + 1);
    }
    return out;
};
export const popIn = defineRule({
    id: "animate/pop-in",
    kind: "lint",
    severity: "warn",
    options: {},
    docs: {
        summary: "An element at least as large as a 70 × 70 px square at 1080p does not appear in a single frame at 80% opacity or more inside a container element that is standing still.",
        why: "Something that becomes fully visible from one frame to the next while everything around it stays still looks like a jump in the video rather than the product responding. The rule checks each element on the frame it first appears, except cursors and elements already on screen in the film's first frame. It reports the element when it is at 80% opacity or more, its visible area is at least 70 × 70 px at 1080p, and its parent element was already on screen in the previous frame and moved less than 0.5 px.",
        fix: "Fade, scale or slide the element in over several frames, or bring it in together with its container so they arrive as one.",
    },
    check({ film, report }) {
        for (const el of film.elements) {
            if (el.role === "cursor" || el.first === film.frames[0])
                continue;
            const s = el.at(el.first);
            if (s.opacity < 0.8 || area(s.visible) < px(film, 70) * px(film, 70))
                continue;
            const parent = s.raw.k.split("/").slice(0, -1).join("/");
            const owner = film.elementOf(el.first, parent);
            const before = film.rows(el.first - 1).some((r) => r.k === parent);
            if (owner && before && owner.step(el.first) < 0.5)
                report({
                    element: el,
                    frames: [el.first, el.first],
                    measured: "appears at 80% opacity or more in a single frame, inside a container that is standing still",
                    need: "an entrance animated over several frames, starting below 80% opacity, or arriving together with its container",
                });
        }
    },
});
export const popOut = defineRule({
    id: "animate/pop-out",
    kind: "lint",
    severity: "error",
    options: { size: 70, settle: 0.1 },
    docs: {
        summary: "An element at least as large as a 70 × 70 px square at 1080p does not disappear in a single frame while it is at 80% opacity or more and its container element stays on screen and still.",
        why: "Something that vanishes at full opacity while nothing around it moves looks like a glitch, because the viewer may have been looking at it and it is suddenly gone. The rule checks an element's last frame: it reports it when, over the preceding 0.1 s, it lost no more than 10% opacity and moved or resized by no more than 4 px at 1080p, while its nearest container element stays above 30% opacity and moves less than 0.5 px in the next frame. It skips cursors, elements still on screen at the end of the film, elements replaced within 0.1 s by an identical copy or by new content in the same place, and frames where more than half of the visible elements disappear together, which is a change of scene.",
        fix: "Fade, scale or slide the element out over several frames, or let it leave together with its container (for example, the window moving off screen, or a change to the next scene).",
        options: {
            size: "The side, in px at 1080p, of the smallest square whose area an element must cover to be checked.",
            settle: "The time, in seconds, before an element's last frame during which it must have stayed still and fully visible for the rule to apply (at least 2 frames). New content that arrives where the element was within this time counts as replacing it.",
        },
    },
    check({ film, options, report }) {
        const end = film.frames[film.frames.length - 1];
        const settle = Math.max(2, framesIn(film, options.settle));
        // A change of scene: most of what's on screen ends in the same frame.
        const cut = new Map();
        const isCut = (f) => {
            if (!cut.has(f)) {
                const shown = film.elements.filter((e) => (e.at(f)?.opacity ?? 0) > 0.3);
                cut.set(f, shown.filter((e) => !e.at(f + 1)).length > 0.5 * shown.length);
            }
            return cut.get(f);
        };
        for (const el of film.elements) {
            if (el.role === "cursor" || el.role === "part" || el.last >= end)
                continue;
            const s = el.at(el.last);
            if (s.opacity < 0.8 ||
                area(s.visible) < px(film, options.size) * px(film, options.size))
                continue;
            // Still, at the end: no fade, move or scale in its last frames.
            const before = el.at(el.last - settle);
            if (!before ||
                before.opacity - s.opacity > 0.1 ||
                Math.max(...s.box.map((v, i) => Math.abs(v - before.box[i]))) >
                    px(film, 4))
                continue;
            // Swapped for an identical copy in the next frame, so nothing visibly changes.
            const twin = film
                .rows(el.last + 1)
                .some((r) => r.t === s.raw.t &&
                r.x === s.raw.x &&
                r.o > 0.8 &&
                r.v.every((v, i) => Math.abs(v - s.visible[i]) <= px(film, 4)));
            // Replaced: new content arrives where it was, or a line or two from it (placeholder lines
            // becoming a paragraph, a value swapping).
            const [x, y, w, h] = s.visible;
            const near = [x, y - 2 * h, w, 5 * h];
            const replaced = film.elements.some((e) => e.first > el.last &&
                e.first <= el.last + settle &&
                area(intersect(e.at(e.first).visible, near)) > 0);
            if (twin || replaced || isCut(el.last))
                continue;
            // Its nearest container: if that goes too, it left with it. Otherwise the container must
            // stay, standing still.
            const parts = s.raw.k.split("/");
            let owner;
            for (let i = parts.length - 1; i > 1 && !owner; i -= 1) {
                const o = film.elementOf(el.last, parts.slice(0, i).join("/"));
                if (o && o !== el)
                    owner = o;
            }
            const next = owner?.at(el.last + 1);
            if (!owner ||
                !next ||
                next.opacity < 0.3 ||
                owner.step(el.last + 1) >= 0.5)
                continue;
            report({
                element: el,
                frames: [el.last, el.last],
                measured: `disappears in a single frame at 80% opacity or more, while its container ${owner.label} stays on screen and still`,
                need: "an exit animated over several frames, or leaving together with its container",
            });
        }
    },
});
export const stagingRules = [
    titleOffCentre,
    noPadding,
    textTooSmall,
    textOverProduct,
    doubleExposure,
    topBanner,
    textHold,
    neverFinishes,
    fadeOnly,
    popIn,
    popOut,
];
