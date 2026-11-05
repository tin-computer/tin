/**
 * A simulated viewer: where a person watching the film is looking, frame by frame, and what
 * they manage to read.
 *
 * - Everything legible is a read: a line of text, followed across frames by its words (it may
 *   move, grow as it's typed, or change a few words). Reading takes time: about 3.5 words a second.
 * - The eye goes where something new happens. A new line, a surface arriving or the cursor
 *   setting off pulls it, unless it's busy with something more compelling (reading wins over small
 *   motion; a big arrival or a headline interrupts reading). A jump costs a moment with no reading.
 * - When nothing new happens, the eye goes to the next unread line (largest type first, then in
 *   reading order), or follows what's moving.
 *
 * Rules then ask what a viewer can't do: text that left before it could be read, too many new
 * things at once in different places, the eye dragged across the frame again and again.
 */
import { area, intersect, isAncestor, readText, union, } from "./film.js";
import { contains } from "./rules/util.js";
export const VIEWER = {
    wordsPerSecond: 3.5,
    legible: 10,
    skimWords: 12,
    asks: 1.5,
    fovea: 0.15,
    saccade: 0.15,
};
const cache = new WeakMap();
const centre = (b) => ({ x: b[0] + b[2] / 2, y: b[1] + b[3] / 2 });
const wordsOf = (text) => text.trim().split(/\s+/).filter(Boolean);
/** Words of reading in a text: real words count once, long tokens (paths, numbers) by length.
 * Letters set as separate spans can lose their spaces, so it's never less than 5 letters a word. */
const cost = (text) => Math.max(wordsOf(text).reduce((sum, w) => sum + Math.max(1, w.length / 7), 0), text.replace(/\s/g, "").length / 5);
/** `a` is `b` with letters missing: the same text part-revealed, or part-gone. */
const within = (a, b) => {
    const x = a.replace(/\s/g, "");
    const y = b.replace(/\s/g, "");
    let i = 0;
    for (const c of y)
        if (c === x[i])
            i += 1;
    return i === x.length;
};
/** Legible text in a frame, grouped into lines and lines into paragraphs. */
const textAt = (film, f, o) => {
    const canvas = [0, 0, film.width, film.height];
    const legible = (o.legible * film.height) / 1080;
    const rows = film
        .rows(f)
        .filter((r) => r.t === "t" &&
        /[A-Za-z0-9]/.test(r.x ?? "") &&
        (r.fs ?? 0) >= legible &&
        r.o > 0.5 &&
        // A cursor's name tag says who it is; it isn't something to read.
        !["label", "part", "cursor"].includes(film.elementOf(f, r.k)?.role ?? "") &&
        area(intersect(r.v, canvas)) > 0.5 * area(r.b))
        .sort((a, b) => a.b[0] - b.b[0]);
    // How each row's opacity is changing: rising as it arrives, falling as it leaves.
    const before = new Map(film.rows(f - 1).map((r) => [r.k, r.o]));
    const trend = (r) => r.o - (before.get(r.k) ?? 0);
    const groups = [];
    for (const r of rows) {
        const fs = r.fs;
        const g = groups.find((l) => {
            const b = union(l.map((x) => x.b));
            const lfs = l[0].fs;
            return (Math.abs(b[1] + b[3] / 2 - (r.b[1] + r.b[3] / 2)) <
                0.5 * Math.max(b[3], r.b[3]) &&
                Math.max(fs, lfs) / Math.min(fs, lfs) < 1.25 &&
                r.b[0] - (b[0] + b[2]) < 1.2 * fs &&
                b[0] - (r.b[0] + r.b[2]) < 1.2 * fs &&
                // Drawn on top of a word already in the line: a different text stacked over it.
                !l.some((x) => area(intersect(x.b, r.b)) > 0.3 * Math.min(area(x.b), area(r.b))));
        });
        if (g)
            g.push(r);
        else
            groups.push([r]);
    }
    const lines = groups.map((rs) => {
        const text = readText(rs);
        return {
            rows: rs,
            text,
            box: union(rs.map((r) => r.b)),
            size: rs[0].fs,
            words: cost(text),
            trend: rs.reduce((sum, r) => sum + trend(r), 0) / rs.length,
        };
    });
    // Paragraphs: lines of one size stacked closely are read as one text (a wrapped headline, a
    // message, a list). A long one is skimmed, not read.
    const paragraphs = [];
    for (const l of [...lines].sort((a, b) => a.box[1] - b.box[1])) {
        const p = paragraphs.find((q) => {
            const b = union(q.map((x) => x.box));
            const size = q[0].size;
            return (Math.max(size, l.size) / Math.min(size, l.size) < 1.2 &&
                // One leaving as another arrives in its place: different texts, not one paragraph. Lines
                // that are both arriving (a caption revealed word by word) or both steady stay together.
                !q.some((x) => (x.trend < -0.01 && l.trend > 0.01) ||
                    (x.trend > 0.01 && l.trend < -0.01)) &&
                l.box[1] - (b[1] + b[3]) < 0.9 * l.size &&
                l.box[0] < b[0] + b[2] &&
                b[0] < l.box[0] + l.box[2]);
        });
        if (p)
            p.push(l);
        else
            paragraphs.push([l]);
    }
    return paragraphs.map((p) => ({
        rows: p.flatMap((l) => l.rows),
        text: p.map((l) => l.text).join(" "),
        box: union(p.map((l) => l.box)),
        size: p[0].size,
        words: Math.min(o.skimWords, p.reduce((sum, l) => sum + l.words, 0)),
        trend: p[0].trend,
    }));
};
export const viewer = (film) => {
    if (cache.has(film))
        return cache.get(film);
    const o = VIEWER;
    const width = film.width;
    const fovea = o.fovea * width;
    const memory = Math.round(film.fps);
    const perFrame = o.wordsPerSecond / film.fps;
    const saccade = Math.round(o.saccade * film.fps);
    const act = film.activity();
    const reads = [];
    const live = [];
    const arrivals = [];
    const gaze = new Map();
    const onsets = new Map();
    const asking = new Map();
    let at = { x: film.width / 2, y: film.height / 2 };
    let target;
    let busyUntil = -1;
    const canvas = [0, 0, film.width, film.height];
    // Already moving where the viewer could see it: not a new event. Something flying in from off
    // the frame is an event when it comes into view.
    const recent = (el, f) => {
        for (let k = 1; k <= Math.max(2, Math.round(film.fps / 5)); k += 1) {
            const s = el.at(f - k);
            if (act.get(f - k)?.moved.has(el) &&
                s &&
                area(intersect(s.visible, canvas)) > 0)
                return true;
        }
        return false;
    };
    for (const f of film.frames) {
        const lines = textAt(film, f, o);
        const painters = new Map(film
            .rows(f)
            .filter((x) => x.t === "b")
            .map((x) => [x.k, x]));
        const seen = new Set();
        const fresh = [];
        // Lines whose exact text is already being read claim their reads first, so a new line can't
        // take over a caption that's still on screen because it shares a few letters with it.
        const known = new Set(live.filter((r) => f - r.to <= memory).map((r) => r.text));
        for (const l of [...lines].sort((a, b) => Number(known.has(b.text)) - Number(known.has(a.text)))) {
            const near = (r) => Math.abs(r.box[0] - l.box[0]) < 2 * l.size &&
                Math.abs(r.box[1] - l.box[1]) < 1.5 * l.size;
            const open = live.filter((r) => !seen.has(r) && f - r.to <= memory);
            // The same words anywhere (moved, re-laid out, faded across); then the same line typed on
            // or changed in a few words, in place.
            let r = open
                .filter((x) => x.text === l.text)
                .sort((a, b) => Math.hypot(a.box[0] - l.box[0], a.box[1] - l.box[1]) -
                Math.hypot(b.box[0] - l.box[0], b.box[1] - l.box[1]))[0];
            if (!r) {
                // Typed on or revealed a letter at a time, or leaving that way.
                const overlaps = (x) => area(intersect(x.box, [
                    l.box[0] - l.size,
                    l.box[1] - l.size,
                    l.box[2] + 2 * l.size,
                    l.box[3] + 2 * l.size,
                ])) > 0;
                r = open.find((x) => overlaps(x) &&
                    ((x.text[0] === l.text[0] && within(x.text, l.text)) ||
                        within(l.text, x.text)));
                if (r && l.text.length > r.text.length)
                    r.asked = f;
            }
            if (!r) {
                r = open.find((x) => {
                    if (!near(x) || x.to < f - 1)
                        return false;
                    const a = wordsOf(x.text);
                    const b = new Set(wordsOf(l.text));
                    return (a.filter((w) => b.has(w)).length >= 0.5 * Math.max(a.length, b.size));
                });
                if (r) {
                    const old = new Set(wordsOf(r.text));
                    r.words += cost(wordsOf(l.text)
                        .filter((w) => !old.has(w))
                        .join(" "));
                    r.asked = f;
                }
            }
            if (!r) {
                r = {
                    id: reads.length,
                    text: l.text,
                    full: l.text,
                    box: l.box,
                    size: l.size,
                    from: f,
                    to: f,
                    words: l.words,
                    read: 0,
                    base: l.words,
                    asked: f,
                    scale: 1,
                    elements: new Set(),
                    boxes: new Map(),
                };
                arrivals.push(r);
                reads.push(r);
                live.push(r);
                fresh.push(r);
            }
            r.text = l.text;
            if (l.text.length > r.full.length)
                r.full = l.text;
            r.box = l.box;
            r.boxes.set(f, l.box);
            r.size = l.size;
            r.to = f;
            r.base = Math.max(r.base, l.words);
            r.words = Math.max(r.words, r.base * r.scale);
            for (const row of l.rows) {
                const el = film.elementOf(f, row.k);
                if (el)
                    r.elements.add(el);
            }
            // Product text sits inside a painted box (anything but a backdrop the frame's size), or is
            // set smaller than display type, in any frame it's on screen.
            const parts = l.rows[0].k.split("/");
            const page = parts
                .map((_, i) => painters.get(parts.slice(0, i).join("/")))
                .find((x) => x &&
                [0, 0, film.width, film.height].some((v, i) => Math.abs(x.b[i] - v) > 2));
            // Unpainted text is still product text unless it's display type: large, and not code.
            const display = l.size >= (32 * film.height) / 1080 &&
                !l.rows.some((x) => /mono|code|courier/i.test(x.ff ?? ""));
            r.surface ??=
                page?.k ?? (display ? undefined : parts.slice(0, 3).join("/"));
            seen.add(r);
        }
        // A page of text arriving together (at once or in a quick stagger) is glanced at as a whole,
        // like a long paragraph.
        for (const page of new Set(fresh.map((r) => r.surface))) {
            if (!page)
                continue;
            const together = arrivals.filter((r) => r.surface === page && f - r.from < film.fps / 2);
            const batch = together.reduce((sum, r) => sum + r.base, 0);
            if (batch > o.skimWords)
                for (const r of together) {
                    r.scale = Math.min(r.scale, o.skimWords / batch);
                    r.words = Math.min(r.words, r.base * r.scale);
                }
        }
        // What pulls the eye this frame.
        const big = (film.height * 40) / 1080;
        const pulls = fresh
            .filter((r) => r.words >= 0.5)
            .map((r) => ({
            box: r.box,
            pull: r.size >= big ? 3 : 2,
            label: `"${r.text.slice(0, 28)}"`,
            read: r,
        }));
        const moved = [...(act.get(f)?.moved ?? [])].filter((el) => !["text", "headline", "label"].includes(el.role));
        const keys = moved.map((el) => el.at(f)?.raw.k ?? el.at(f - 1)?.raw.k ?? "");
        for (const [i, el] of moved.entries()) {
            if (keys.some((k, j) => j !== i && k && isAncestor(k, keys[i])))
                continue;
            if (recent(el, f))
                continue;
            const s = el.at(f) ?? el.at(f - 1);
            if (!s)
                continue;
            // How big it is, once something of it is in view: a card sliding in is a card-sized event.
            const share = area(intersect(s.visible, canvas)) > 0
                ? Math.min(area(s.box), film.area) / film.area
                : 0;
            const pull = el.role === "cursor" ? 2 : share >= 0.05 ? 3 : share >= 0.003 ? 1 : 0;
            if (pull)
                pulls.push({ box: s.visible, pull, label: el.label, element: el });
        }
        if (pulls.length)
            onsets.set(f, pulls);
        // Where the eye goes.
        const onScreen = (r) => !!r && seen.has(r);
        const unread = (r) => !!r && r.words - r.read > 0.25;
        const engaged = target?.read
            ? onScreen(target.read) &&
                (unread(target.read) || f - target.read.asked < film.fps / 2)
                ? 2
                : 0
            : target?.element && act.get(f)?.moved.has(target.element)
                ? 1
                : 0;
        const look = (next, box) => {
            const c = centre(box);
            if (Math.hypot(c.x - at.x, c.y - at.y) > fovea)
                busyUntil = f + saccade;
            target = next;
        };
        // Moving whatever the eye is already on (a camera move, a window sliding over) is followed,
        // not a new place to look.
        const held = target?.read?.box ?? target?.element?.at(f)?.visible;
        const strongest = pulls
            .filter((p) => !(held &&
            p.element &&
            !p.read &&
            p.element.at(f - 1) &&
            contains(p.box, held)))
            .sort((a, b) => b.pull - a.pull ||
            Math.hypot(centre(a.box).x - at.x, centre(a.box).y - at.y) -
                Math.hypot(centre(b.box).x - at.x, centre(b.box).y - at.y))[0];
        if (strongest && strongest.pull > engaged)
            look({
                read: strongest.read,
                element: strongest.element,
                pull: strongest.pull,
            }, strongest.box);
        else if (!engaged) {
            const next = [...seen]
                .filter((r) => unread(r))
                .sort((a, b) => b.size - a.size || a.box[1] - b.box[1] || a.box[0] - b.box[0])[0];
            if (next)
                look({ read: next, pull: 2 }, next.box);
            else {
                const mover = moved
                    .map((el) => ({ el, s: el.at(f) }))
                    .filter((m) => m.s)
                    .sort((a, b) => area(b.s.visible) - area(a.s.visible))[0];
                if (mover && mover.el !== target?.element)
                    look({ element: mover.el, pull: 1 }, mover.s.visible);
            }
        }
        if (target?.read && onScreen(target.read) && f >= busyUntil)
            target.read.read = Math.min(target.read.words, target.read.read + perFrame);
        const box = target?.read && onScreen(target.read)
            ? target.read.box
            : target?.element?.at(f)?.visible;
        if (box)
            at = centre(box);
        asking.set(f, [...seen].filter((r) => r.scale === 1 &&
            r.words >= 2 &&
            unread(r) &&
            f - r.asked < o.asks * film.fps));
        gaze.set(f, {
            ...at,
            label: target?.read
                ? `"${target.read.text.slice(0, 28)}"`
                : (target?.element?.label ?? "nothing"),
            read: target?.read,
            element: target?.element,
        });
    }
    const out = { reads, gaze, onsets, asking };
    cache.set(film, out);
    return out;
};
