/**
 * Spacing: the room around things. A large surface's edge is either well inside the canvas or
 * clearly cut off by it, never just at it; text outside any surface is either centred in the
 * space beside it or lined up with something.
 */
import { area, intersect, isAncestor } from "../film.js";
import { defineRule } from "../rule.js";
import { viewer } from "../viewer.js";
import { frames as framesIn, px, runs } from "./util.js";
/** Whether a box is standing still (moving under 2 px a frame), so its position is deliberate rather than part of a move. */
const held = (el, f) => {
    const s = el.at(f);
    const p = el.at(f - 1);
    return !!s && !!p && s.box.every((v, i) => Math.abs(v - p.box[i]) < 2);
};
export const ambiguousCrop = defineRule({
    id: "framing/ambiguous-crop",
    kind: "lint",
    severity: "warn",
    options: { room: 32, crop: 0.06, subject: 0.15, seconds: 0.5 },
    docs: {
        summary: "Each edge of a large surface (a window, card or panel) that is standing still is either at least 32 px at 1080p inside the canvas edge, or past the canvas edge by more than 6% of the surface's width (left and right edges) or height (top and bottom edges).",
        why: "A surface with space around it looks deliberately placed, and one that runs well past the canvas edge looks like a deliberate close-up. An edge just inside the canvas or just past it looks like a mistake, as if the surface didn't quite fit. The rule checks surfaces above 90% opacity that cover at least 15% of the canvas and have moved or resized by less than 2 px since the previous frame, and reports an edge that stays in between for at least 0.5 s.",
        fix: "Either zoom out or move the surface until the edge is at least the required distance inside the canvas, or zoom in until the edge is well past the canvas edge.",
        options: {
            room: "The smallest distance, in px at 1080p, a surface's edge must be inside the canvas edge.",
            crop: "How far past the canvas edge a surface's edge must be to count as clearly cut off, as a fraction of the surface's width (left and right edges) or height (top and bottom edges).",
            subject: "The smallest fraction of the canvas (0.15 is 15%) a surface must cover to be checked.",
            seconds: "The shortest time, in seconds, an edge must stay in between to be reported.",
        },
    },
    check({ film, options, report }) {
        const room = px(film, options.room);
        const hits = new Map();
        for (const el of film.elements.filter((e) => e.role === "surface")) {
            for (const f of el.frames) {
                const s = el.at(f);
                if (s.opacity < 0.9 ||
                    !held(el, f) ||
                    area(intersect(s.visible, [0, 0, film.width, film.height])) <
                        options.subject * film.area)
                    continue;
                const [x, y, w, h] = s.box;
                // Signed distance to each canvas edge: positive inside the canvas, negative past its edge.
                const edges = [
                    ["left", x, w],
                    ["top", y, h],
                    ["right", film.width - (x + w), w],
                    ["bottom", film.height - (y + h), h],
                ];
                for (const [edge, m, size] of edges)
                    if (m < room && m > -options.crop * size)
                        hits.set(el, [...(hits.get(el) ?? []), [f, edge, m]]);
            }
        }
        for (const [el, list] of hits)
            for (const edge of ["left", "top", "right", "bottom"]) {
                const mine = list.filter((h) => h[1] === edge);
                for (const [a, b] of runs(mine.map((h) => h[0]), 2)) {
                    if (b - a + 1 < framesIn(film, options.seconds))
                        continue;
                    const m = mine.find((h) => h[0] === a)[2];
                    report({
                        element: el,
                        frames: [a, b],
                        measured: `its ${edge} edge is ${m >= 0 ? `${m.toFixed(0)} px inside` : `${(-m).toFixed(0)} px past`} the canvas edge`,
                        need: `the edge at least ${options.room} px at 1080p inside the canvas edge, or past it by more than ${(options.crop * 100).toFixed(0)}% of the surface's width or height`,
                    });
                }
            }
    },
});
export const unbalancedSpace = defineRule({
    id: "layout/unbalanced-space",
    kind: "lint",
    severity: "warn",
    options: { ratio: 1.6, align: 10, seconds: 0.5, minGap: 24 },
    docs: {
        summary: "Text that isn't inside a surface (a window, card or panel), such as a caption or headline, either has a larger side gap of no more than 1.6 times the smaller one, or has its left or right edge within 10 px at 1080p of another element's edge or centre, or of the canvas's.",
        why: "Viewers notice the space on each side of a line of text. Equal space, or an edge lined up with something else, looks intentional; uneven space with nothing to line up with looks accidental. For text of at least two words that has moved less than 2 px since the previous frame, the rule measures the gap from its left and right edges to the nearest element beside it at the same height, or to the canvas edge; small elements within one font size of the text and no more than twice its height, such as a logo beside a name, count as part of it. It reports when the larger gap is at least 24 px at 1080p and more than 1.6 times the smaller, nothing lines up, the text isn't centred on the canvas, and this lasts at least 0.5 s.",
        fix: "Centre the text between the elements on either side of it (or the canvas edge), or move it so its left or right edge lines up with the edge or centre of another element. The finding gives both gaps.",
        options: {
            ratio: "How many times larger one side gap may be than the other before the text counts as unbalanced.",
            align: "How close, in px at 1080p, an edge of the text must be to another element's edge or centre, or the canvas's, to count as lined up with it.",
            seconds: "The shortest time, in seconds, the problem must last to be reported.",
            minGap: "The smallest size, in px at 1080p, the larger gap must have for the text to be checked.",
        },
    },
    check({ film, options, report }) {
        const v = viewer(film);
        const tol = px(film, options.align);
        const hits = new Map();
        for (const r of v.reads.filter((x) => !x.surface && x.words >= 1.5)) {
            for (let f = r.from; f <= r.to; f += 1) {
                const own = r.boxes.get(f);
                if (!own)
                    continue;
                // Its letters and words in this frame, and whether they're standing still.
                const mine = [...r.elements].filter((e) => {
                    const s = e.at(f);
                    return !!s && area(intersect(s.visible, own)) > 0.5 * area(s.visible);
                });
                const el = mine[0];
                if (!el || mine.some((e) => !held(e, f)))
                    continue;
                const keys = mine.map((e) => e.at(f).raw.k);
                const others = film.elements
                    .filter((e) => e.role !== "cursor" &&
                    e.role !== "part" &&
                    e.role !== "label" &&
                    !mine.includes(e))
                    .map((e) => e.at(f))
                    .filter((s) => !!s &&
                    s.opacity > 0.3 &&
                    area(s.visible) > 0.002 * film.area &&
                    area(s.visible) < 0.9 * film.area &&
                    area(intersect(s.visible, own)) < 0.5 * area(s.visible) &&
                    !keys.some((k) => isAncestor(s.raw.k, k)))
                    .map((s) => s.visible);
                // Something its own height within a letter's height of it belongs with it (a logo beside
                // its name), not a window beside a caption.
                let box = own;
                for (let grew = true, n = 0; grew && n < 20; n += 1) {
                    grew = false;
                    for (const o of others) {
                        const near = o[1] < box[1] + box[3] &&
                            o[3] <= 2 * own[3] &&
                            box[1] < o[1] + o[3] &&
                            Math.max(o[0] - (box[0] + box[2]), box[0] - (o[0] + o[2])) <
                                r.size &&
                            !(o[0] >= box[0] &&
                                o[0] + o[2] <= box[0] + box[2] &&
                                o[1] >= box[1] &&
                                o[1] + o[3] <= box[1] + box[3]);
                        if (near) {
                            const x0 = Math.min(box[0], o[0]);
                            const y0 = Math.min(box[1], o[1]);
                            box = [
                                x0,
                                y0,
                                Math.max(box[0] + box[2], o[0] + o[2]) - x0,
                                Math.max(box[1] + box[3], o[1] + o[3]) - y0,
                            ];
                            grew = true;
                        }
                    }
                }
                const group = box;
                const band = (b) => b[1] < group[1] + group[3] &&
                    group[1] < b[1] + b[3] &&
                    area(intersect(b, group)) === 0;
                const left = Math.max(0, ...others
                    .filter((b) => band(b) && b[0] + b[2] <= box[0] + 1)
                    .map((b) => b[0] + b[2]));
                const right = Math.min(film.width, ...others
                    .filter((b) => band(b) && b[0] >= box[0] + box[2] - 1)
                    .map((b) => b[0]));
                const gaps = [
                    box[0] - left,
                    right - (box[0] + box[2]),
                ];
                const [lo, hi] = [Math.min(...gaps), Math.max(...gaps)];
                if (hi < px(film, options.minGap) ||
                    hi <= options.ratio * Math.max(lo, 1))
                    continue;
                // Lined up with something (an edge or a centre), or centred on the canvas: intended.
                const lines = [
                    ...others,
                    [0, 0, film.width, film.height],
                ].flatMap((b) => [b[0], b[0] + b[2], b[0] + b[2] / 2]);
                if ([box[0], box[0] + box[2]].some((x) => lines.some((l) => Math.abs(l - x) <= tol)) ||
                    Math.abs(box[0] + box[2] / 2 - film.width / 2) <= tol)
                    continue;
                const key = `${r.id}`;
                const h = hits.get(key) ?? { frames: [], text: r.full, el, gaps };
                h.frames.push(f);
                hits.set(key, h);
            }
        }
        for (const h of hits.values())
            for (const [a, b] of runs(h.frames, 2)) {
                if (b - a + 1 < framesIn(film, options.seconds))
                    continue;
                report({
                    element: h.el,
                    frames: [a, b],
                    measured: `"${h.text.slice(0, 30)}" has ${h.gaps[0].toFixed(0)} px of space on its left and ${h.gaps[1].toFixed(0)} px on its right, and neither edge lines up with another element or the canvas`,
                    need: `the larger gap no more than ${options.ratio} times the smaller, or an edge within ${options.align} px at 1080p of another element's edge or centre`,
                });
            }
    },
});
export const spacingRules = [ambiguousCrop, unbalancedSpace];
