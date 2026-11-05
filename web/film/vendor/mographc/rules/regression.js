/**
 * Regressions: what got measurably worse compared with the saved baseline (usually the version a
 * person last approved). Parts of the two films are paired by their headlines, in order, like a diff.
 */
import { area, intersect } from "../film.js";
import { defineRule } from "../rule.js";
const letters = (s) => {
    const m = new Map();
    for (const c of s.toLowerCase().replace(/\s/g, ""))
        m.set(c, (m.get(c) ?? 0) + 1);
    return m;
};
const similarity = (a, b) => {
    const ca = letters(a);
    const cb = letters(b);
    let common = 0;
    for (const [c, n] of ca)
        common += Math.min(n, cb.get(c) ?? 0);
    return (common /
        Math.max(a.replace(/\s/g, "").length, b.replace(/\s/g, "").length, 1));
};
/** Pair two films' headlines in order, so a reworded headline still pairs with its earlier version. */
export const alignBeats = (cur, base) => {
    const n = cur.length;
    const m = base.length;
    const best = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i -= 1)
        for (let j = m - 1; j >= 0; j -= 1) {
            const s = similarity(cur[i].text, base[j].text);
            best[i][j] = Math.max(best[i + 1][j], best[i][j + 1], s >= 0.5 ? best[i + 1][j + 1] + s : 0);
        }
    const pairs = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
        const s = similarity(cur[i].text, base[j].text);
        if (s >= 0.5 && best[i][j] === best[i + 1][j + 1] + s) {
            pairs.push([cur[i], base[j]]);
            i += 1;
            j += 1;
        }
        else if (best[i][j] === best[i + 1][j])
            i += 1;
        else
            j += 1;
    }
    return pairs;
};
/** Small text on screen for at least a quarter of the frames from `from` to `to`. */
const inventory = (film, from, to) => {
    const seen = new Map();
    let n = 0;
    for (let f = from; f <= to; f += 2) {
        n += 1;
        const here = new Set(film
            .rows(f)
            .filter((r) => r.t === "t" &&
            r.o > 0.5 &&
            (r.x ?? "").length > 3 &&
            (r.fs ?? 0) < 40 &&
            area(intersect(r.v, [0, 0, film.width, film.height])) > 0)
            .map((r) => r.x));
        for (const x of here)
            seen.set(x, (seen.get(x) ?? 0) + 1);
    }
    return new Set([...seen].filter(([, c]) => c >= 0.25 * n).map(([x]) => x));
};
const rendered = (film, from, to) => {
    const out = new Set();
    for (let f = from; f <= to; f += 2)
        for (const r of film.rows(f))
            if (r.t === "t" && r.x)
                out.add(r.x);
    return out;
};
export const contentRemoved = defineRule({
    id: "regression/content-removed",
    kind: "lint",
    severity: "error",
    needsBaseline: true,
    options: {},
    docs: {
        summary: "Compared with the saved baseline, while each headline is on screen the film shows the same smaller texts (under 40 px, such as labels and list rows) as the baseline did, with at most one of them missing.",
        why: "Labels, list rows or sidebar sections that disappear from a part of the film a person already approved make that part emptier, and that is rarely a change anyone meant to make. The rule pairs each headline in the film with the same headline in the baseline, in order, allowing for rewording (at least 50% of letters in common). For each pair it lists the texts in the baseline that are longer than 3 characters, have a font size under 40 px, are above 50% opacity and are on screen in at least 25% of the headline's time on screen (checked every second frame), and reports the ones the film no longer shows during its headline's time. A shortened text that keeps at least 60% of the original's length still counts as shown. Parts of the film where no headline is on screen are not compared.",
        fix: "Restore the missing texts. If removing them was intended, accept the finding with a time range and a reason in the config, or save a new baseline with `mographc baseline save`. When more than 40% of the baseline's texts are missing, that part of the film was probably redesigned rather than trimmed, so the finding is only a warning asking a person to look at the new version.",
    },
    check({ film, baseline, report }) {
        if (!baseline)
            return;
        for (const [h, g] of alignBeats(film.headlines(), baseline.headlines())) {
            const now = inventory(film, h.from, h.to);
            const kept = new Set([...now, ...rendered(film, h.from, h.to)]);
            const before = inventory(baseline, g.from, g.to);
            // A truncated label still counts as kept; a short word inside a longer one doesn't.
            const lost = [...before].filter((x) => !now.has(x) &&
                ![...kept].some((y) => (x.includes(y) || y.includes(x)) &&
                    Math.min(x.length, y.length) >=
                        0.6 * Math.max(x.length, y.length)));
            if (lost.length < 2)
                continue;
            const wholesale = lost.length > 0.4 * before.size;
            report({
                frames: [h.from, h.to],
                measured: wholesale
                    ? `${lost.length} of the ${before.size} texts the baseline showed with the headline "${g.text.slice(0, 24)}" are missing, more than 40% of them, so this part of the film was probably redesigned`
                    : `${lost.length} texts the baseline showed with the headline "${g.text.slice(0, 24)}" are missing: ${lost
                        .slice(0, 4)
                        .map((x) => `"${x.slice(0, 24)}"`)
                        .join(", ")}`,
                need: wholesale
                    ? "a person checks the new version of this part of the film and, if it is right, saves a new baseline"
                    : "at most 1 of the baseline's texts missing while this headline is on screen",
                severity: wholesale ? "warn" : "error",
            });
        }
    },
});
const cursorCast = (film) => {
    const cs = film.elements.filter((e) => e.role === "cursor");
    const sizes = cs
        .map((e) => {
        const s = e.frames
            .map((f) => Math.max(e.at(f).box[2], e.at(f).box[3]))
            .sort((a, b) => a - b);
        return s[Math.floor(s.length / 2)];
    })
        .sort((a, b) => a - b);
    const fast = cs
        .flatMap((e) => e.frames
        .filter((f) => e.step(f) * film.fps >= 600)
        .map((f) => e.at(f).stretch))
        .sort((a, b) => a - b);
    const top = fast.slice(-Math.max(1, Math.floor(fast.length / 10)));
    return {
        sizes,
        stretch: top.length ? top.reduce((a, b) => a + b, 0) / top.length : 1,
    };
};
export const smaller = defineRule({
    id: "regression/cursor-smaller",
    kind: "lint",
    severity: "error",
    needsBaseline: true,
    options: { tolerance: 0.1 },
    docs: {
        summary: "Compared with the saved baseline, neither the film's largest cursor nor its smallest cursor is more than 10% smaller than the largest or smallest cursor in the baseline.",
        why: "The cursor is often the element the viewer follows, so making it smaller without a reason makes it harder to follow. A cursor's size is the median, over the frames it is in, of the longer side of its box in px. The rule compares the largest cursor in the film with the largest in the baseline, and the smallest with the smallest.",
        fix: "Restore the cursor's size from the baseline. If the change was deliberate, accept the finding with a time range and a reason in the config, or save a new baseline with `mographc baseline save`.",
        options: {
            tolerance: "How much smaller than in the baseline a cursor may be, as a share of the baseline's size from 0 to 1 (0.1 allows 10% smaller).",
        },
    },
    check({ film, baseline, options, report }) {
        if (!baseline)
            return;
        const now = cursorCast(film).sizes;
        const before = cursorCast(baseline).sizes;
        if (!now.length || !before.length)
            return;
        for (const [role, a, b] of [
            ["largest", now[now.length - 1], before[before.length - 1]],
            ["smallest", now[0], before[0]],
        ])
            if (a < (1 - options.tolerance) * b)
                report({
                    frames: [0, film.durationInFrames - 1],
                    measured: `The ${role} cursor is ${a.toFixed(0)} px along its longer side (median over its frames)`,
                    need: `at least ${((1 - options.tolerance) * 100).toFixed(0)}% of the baseline's ${b.toFixed(0)} px`,
                });
    },
});
export const lessStretch = defineRule({
    id: "regression/less-stretch",
    kind: "lint",
    severity: "error",
    needsBaseline: true,
    options: {},
    docs: {
        summary: "Compared with the saved baseline, the cursors' stretch while moving fast (averaged over the fastest 10% of frames at 600 px/s or more) is no more than 0.04 lower than in the baseline.",
        why: "Stretching a fast cursor along its direction of travel makes its speed easier to follow; with less stretch than the version a person approved, a fast cursor looks stiffer. The rule takes every frame in which a cursor moves at 600 px/s or more, keeps the 10% of those frames with the most stretch (at least one), and averages their stretch. With no such frames the value is 1, meaning no stretch.",
        fix: "Restore the stretch from the baseline, and check that the cursor still moves as fast as it did, since slower moves have no frames to measure.",
    },
    check({ film, baseline, report }) {
        if (!baseline)
            return;
        const a = cursorCast(film).stretch;
        const b = cursorCast(baseline).stretch;
        if (a < b - 0.04)
            report({
                frames: [0, film.durationInFrames - 1],
                measured: `The cursors' average stretch over their most stretched fast frames is ${a.toFixed(2)}`,
                need: `a stretch no more than 0.04 below the baseline's ${b.toFixed(2)}`,
            });
    },
});
export const weakerEaseOut = defineRule({
    id: "regression/weaker-ease-out",
    kind: "lint",
    severity: "error",
    needsBaseline: true,
    options: {},
    docs: {
        summary: "Compared with the saved baseline, the main move while each headline is on screen has an ease-out strength no more than 0.05 lower than the same move in the baseline.",
        why: "When the same move slows down less before it stops than it did in the version a person approved, people notice the film got worse even if they can't say why. The rule pairs each headline in the film with the same headline in the baseline, in order, allowing for rewording. For each, the main move is the move by the largest element (by visible area, and covering more than 5% of the canvas, cursors excluded) that starts between 10 frames before the headline appears and when it leaves, lasts at least 0.25 s, travels more than 80 px, and ends with the element on the canvas. The rule compares the ease-out strength (the share of the move's time spent covering its last 20% of distance) of the two main moves.",
        fix: "Restore the easing curve from the baseline on that move, so it spends as much of its time slowing down before it stops.",
    },
    check({ film, baseline, report }) {
        if (!baseline)
            return;
        const main = (f, from, to) => f.elements
            .filter((e) => e.role !== "cursor")
            .flatMap((e) => e
            .moves()
            .filter((m) => m.lands &&
            m.from >= from - 10 &&
            m.from <= to &&
            m.seconds >= 0.25 &&
            m.distance > 80)
            .map((m) => ({ m, size: area(e.at(m.from).visible), e })))
            .filter((x) => x.size > 0.05 * f.area)
            .sort((a, b) => b.size - a.size)[0];
        for (const [h, g] of alignBeats(film.headlines(), baseline.headlines())) {
            const a = main(film, h.from, h.to);
            const b = main(baseline, g.from, g.to);
            if (a && b && a.m.easeOut < b.m.easeOut - 0.05)
                report({
                    element: a.e,
                    frames: [a.m.from, a.m.to],
                    measured: `makes the main move while this headline is on screen with an ease-out strength of ${a.m.easeOut.toFixed(2)}`,
                    need: `an ease-out strength no more than 0.05 below the baseline's ${b.m.easeOut.toFixed(2)} for the same move`,
                });
        }
    },
});
export const regressionRules = [
    contentRemoved,
    smaller,
    lessStretch,
    weakerEaseOut,
];
