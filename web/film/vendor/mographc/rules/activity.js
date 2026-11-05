/**
 * Pace and framing: how much of the film is changing, and whether the view is zoomed in on
 * where the change is. Calibrated on product films people judged: the ones that held attention
 * were never still for more than about 0.9 s and had something changing about 65% of the time
 * (slow drift aside) with a large change half the time; films judged boring sat near 40%.
 */
import { area } from "../film.js";
import { defineRule } from "../rule.js";
import { runs, surfacesAt } from "./util.js";
/** Frames where at least `share` of the canvas changes. */
const activeFrames = (film, share, substantial = false) => {
    const act = film.activity();
    return film.frames.filter((f) => ((substantial ? act.get(f)?.bigShare : act.get(f)?.share) ?? 0) >= share);
};
export const activity = defineRule({
    id: "pace/activity",
    kind: "lint",
    severity: "error",
    options: { share: 0.01, warnBelow: 0.6, errorBelow: 0.45 },
    docs: {
        summary: "On at least 60% of the film's frames, something changes over at least 1% of the canvas (a warning below 60%, an error below 45%).",
        why: "When nothing changes on screen, viewers tend to stop watching, and the film looks like a slideshow. Films that hold attention have something changing almost all the time: elements appearing one by one, the next element arriving, or a zoom toward what matters next. A frame counts as changing when elements covering at least 1% of the canvas move faster than 45 px/s at 1080p, resize, fade, change their text, appear or disappear. Slow, steady movement (see `camera/creep`) doesn't count.",
        fix: "Split long sections into shorter ones that each show one thing, bring elements in one at a time instead of showing them complete, and bring in the next element while the viewer reads text. A slow zoom or drift doesn't count as change.",
        options: {
            share: "The minimum share of the canvas (0.01 is 1%) that must change for a frame to count as changing.",
            warnBelow: "The share of frames (0.6 is 60%) that must be changing; below it the rule reports a warning.",
            errorBelow: "The share of changing frames below which the finding is an error instead of a warning.",
        },
    },
    check({ film, options, report }) {
        const share = activeFrames(film, options.share).length / film.frames.length;
        if (share >= options.warnBelow)
            return;
        report({
            frames: [film.frames[0], film.frames[film.frames.length - 1]],
            measured: `something changes over at least 1% of the canvas on only ${(share * 100).toFixed(0)}% of the frames`,
            need: `something changing on at least ${(options.warnBelow * 100).toFixed(0)}% of the frames`,
            severity: share < options.errorBelow ? "error" : "warn",
        });
    },
});
export const deadAir = defineRule({
    id: "pace/dead-air",
    kind: "lint",
    severity: "error",
    options: { share: 0.01, warnAfter: 1.0, errorAfter: 2.0, endHold: 2.5 },
    docs: {
        summary: "The film never goes more than 1.0 s without something changing over at least 1% of the canvas (an error after 2.0 s), except for a still ending of up to 2.5 s.",
        why: "A picture that stays frozen looks as if the video has stalled. A frame counts as changing when elements covering at least 1% of the canvas move faster than 45 px/s at 1080p, resize, fade, change their text, appear or disappear. A slow zoom or drift doesn't count, because it doesn't show the viewer anything new. The end of the film, for example a logo or end card, may stay still for up to 2.5 s.",
        fix: "Add change to the still stretch (bring in the next element, reveal a detail, or animate part of what's shown), zoom toward the next thing that matters with an eased move, or start the next section sooner.",
        options: {
            share: "The minimum share of the canvas (0.01 is 1%) that must change for a frame to count as changing.",
            warnAfter: "The longest time, in seconds, the film may go without change before the rule reports a warning.",
            errorAfter: "The time, in seconds, without change after which the finding is an error instead of a warning.",
            endHold: "How long, in seconds, the film may stay unchanged at its very end without a finding.",
        },
    },
    check({ film, options, report }) {
        const act = film.activity();
        const still = film.frames.filter((f) => (act.get(f)?.share ?? 0) < options.share);
        const last = film.frames[film.frames.length - 1];
        for (const [a, b] of runs(still, 1)) {
            const seconds = (b - a + 1) / film.fps;
            if (seconds <= options.warnAfter)
                continue;
            if (b >= last - 1 && seconds <= options.endHold)
                continue;
            report({
                frames: [a, b],
                measured: `nothing changes over 1% or more of the canvas for ${seconds.toFixed(1)} s`,
                need: `no more than ${options.warnAfter} s without change`,
                severity: seconds > options.errorAfter ? "error" : "warn",
            });
        }
    },
});
export const longShot = defineRule({
    id: "pace/long-shot",
    kind: "lint",
    severity: "warn",
    options: { bigChange: 0.08, seconds: 3.5 },
    docs: {
        summary: "At least every 3.5 s, something changes substantially over at least 8% of the canvas.",
        why: "When the same layout stays on screen while three things happen in it one after another, the film feels like a slideshow. Three short sections that each show one thing keep the film moving and give each idea its own moment. A change counts as substantial when an element moves faster than 45 px/s at 1080p, resizes, appears, disappears, fades quickly or changes its text; small fades and slow drift don't count.",
        fix: "Give each thing the layout shows its own section, or zoom in on the part that changes and move across to the next element, instead of changing details inside a layout that stays still.",
        options: {
            bigChange: "The minimum share of the canvas (0.08 is 8%) that must change substantially for a frame to count as a large change.",
            seconds: "The longest time, in seconds, allowed between two large changes (or between the start or end of the film and the nearest one).",
        },
    },
    check({ film, options, report }) {
        const big = activeFrames(film, options.bigChange, true);
        const marks = [film.frames[0], ...big, film.frames[film.frames.length - 1]];
        for (let i = 1; i < marks.length; i += 1) {
            const seconds = (marks[i] - marks[i - 1]) / film.fps;
            if (seconds > options.seconds)
                report({
                    frames: [marks[i - 1], marks[i]],
                    measured: `for ${seconds.toFixed(1)} s, nothing changes substantially over a large part of the canvas`,
                    need: `a change over at least ${(options.bigChange * 100).toFixed(0)}% of the canvas at least every ${options.seconds} s`,
                });
        }
    },
});
export const smallAction = defineRule({
    id: "staging/small-action",
    kind: "lint",
    severity: "warn",
    options: { region: 0.06, surface: 0.25, seconds: 0.8 },
    docs: {
        summary: "While a surface covering 25% to 95% of the canvas is visible, the substantial changes on screen are not all inside one region of 6% of the canvas or less for 0.8 s or longer. When they are, the view should zoom in on that region.",
        why: "If someone is typing in an input at the bottom of a large, otherwise empty window, the viewer has nothing to look at in the rest of the canvas, and the text being typed is small. Zooming in on the input makes it large and shows only what matters. The rule takes, on each frame, the rectangle around everything that moves faster than 45 px/s at 1080p, resizes, appears, disappears, fades quickly or changes its text, and checks whether a surface (a window, card or panel) covering 25% to 95% of the canvas is visible at the same time.",
        fix: "Zoom in by scaling the whole film's content about the region that's changing, or crop to it, then zoom back out when something happens elsewhere. The finding gives the region's size and position in px.",
        options: {
            region: "The largest share of the canvas (0.06 is 6%) the changing region may cover for the rule to apply.",
            surface: "The smallest share of the canvas (0.25 is 25%) a visible surface must cover for the rule to apply.",
            seconds: "How long, in seconds, the change must stay inside a small region before the rule reports it.",
        },
    },
    check({ film, options, report }) {
        const act = film.activity();
        const hits = [];
        for (const f of film.frames) {
            // Slow drift doesn't show anything: judge where the substantial change is.
            const a = act.get(f);
            if (!a?.bigRegion || a.bigShare <= 0)
                continue;
            const surfaces = surfacesAt(film, f, options.surface, 0.95);
            if (!surfaces.length || area(a.bigRegion) > options.region * film.area)
                continue;
            hits.push([f, a.bigRegion]);
        }
        for (const [a, b] of runs(hits.map((h) => h[0]), 3)) {
            if ((b - a + 1) / film.fps < options.seconds)
                continue;
            // The typical region, not their union: the change may move around inside the large surface.
            const boxes = hits
                .filter((h) => h[0] >= a && h[0] <= b)
                .map((h) => h[1])
                .sort((p, q) => area(p) - area(q));
            const r = boxes[Math.floor(boxes.length / 2)];
            report({
                frames: [a, b],
                measured: `for ${((b - a + 1) / film.fps).toFixed(1)} s, everything that changes is inside a ${r[2].toFixed(0)}x${r[3].toFixed(0)} px region at (${r[0].toFixed(0)}, ${r[1].toFixed(0)}) while a large surface is visible`,
                need: `a view zoomed in or cropped so the changing region covers more than ${(options.region * 100).toFixed(0)}% of the canvas`,
            });
        }
    },
});
export const creep = defineRule({
    id: "camera/creep",
    kind: "lint",
    severity: "error",
    options: { seconds: 1.2, area: 0.03 },
    docs: {
        summary: "Elements covering at least 3% of the canvas don't move or zoom slowly (slower than 45 px/s at 1080p and under 0.6% of their size per frame) for 1.2 s or longer.",
        why: "A slow, steady zoom or pan makes the picture move without showing the viewer anything new, so it looks like filler. A zoom or pan should go toward something, such as the part of the canvas where something is happening, with an eased curve, and then come to rest. The slow end of a real move that just happened is not reported. `pace/activity` and `pace/dead-air` don't count this slow movement as change.",
        fix: "Replace the slow movement with real change (bring in the next element, or reveal a detail), or make it an eased zoom or pan toward something specific that then comes to rest.",
        options: {
            seconds: "How long, in seconds, the slow movement must last before the rule reports it.",
            area: "The minimum share of the canvas (0.03 is 3%) the slowly moving elements must cover together.",
        },
    },
    check({ film, options, report }) {
        const act = film.activity();
        // Frames where something sizeable drifts: a slow zoom or pan on the frame, a title, a card.
        const creeping = film.frames.filter((f) => (act.get(f)?.drift?.share ?? 0) >= options.area);
        // A sub-pixel drift sometimes rounds to no motion for a frame or two: bridge those.
        for (const [a, b] of runs(creeping, Math.max(2, Math.round(film.fps / 10)))) {
            const seconds = (b - a + 1) / film.fps;
            if (seconds < options.seconds)
                continue;
            // What drifted most of the time.
            const count = new Map();
            for (let f = a; f <= b; f += 1)
                for (const el of act.get(f)?.drift?.elements ?? [])
                    if (el.role !== "cursor")
                        count.set(el, (count.get(el) ?? 0) + 1);
            const main = [...count].sort((x, y) => y[1] - x[1])[0]?.[0];
            if (!main)
                continue;
            // The long, slow end of a real move isn't reported: it carries on from that move and slows
            // to a stop. Slow movement that is reported speeds up on its own, or keeps its pace.
            const rates = [];
            for (let f = a; f <= b; f += 1) {
                const s = main.at(f);
                const p = main.at(f - 1);
                if (s && p)
                    rates.push(Math.max(...s.box.map((v, i) => Math.abs(v - p.box[i]))));
            }
            const span = Math.max(1, Math.round(film.fps / 6));
            const smooth = rates.map((_, i) => rates.slice(i, i + span).reduce((x, y) => x + y, 0) / span);
            const settling = film.frames.some((f) => f < a &&
                f >= a - Math.round(film.fps / 5) &&
                act.get(f)?.moved.has(main));
            if (settling &&
                Math.max(...smooth) < 1.5 * smooth[0] &&
                smooth[smooth.length - span] < smooth[0] / 3)
                continue;
            const s0 = main.at(a) ?? main.at(main.first);
            const s1 = main.at(b) ?? main.at(main.last);
            const zoom = ((s1.box[2] / s0.box[2]) ** (1 / seconds) - 1) * 100;
            report({
                element: main,
                frames: [a, b],
                measured: `moves slowly and steadily for ${seconds.toFixed(1)} s${Math.abs(zoom) > 0.1 ? `, zooming ${zoom.toFixed(1)}% a second` : ""}, slower than 45 px/s at 1080p`,
                need: "either an eased move faster than 45 px/s at 1080p that comes to rest, or no movement",
            });
        }
    },
});
export const activityRules = [activity, deadAir, longShot, smallAction, creep];
