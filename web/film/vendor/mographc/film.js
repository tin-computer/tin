/** Tag vocabulary: the names authors reach for, mapped to the roles rules understand. */
const ROLE_ALIASES = {
    cursor: "cursor",
    pointer: "cursor",
    window: "surface",
    card: "surface",
    panel: "surface",
    sheet: "surface",
    modal: "surface",
    popover: "surface",
    surface: "surface",
    page: "surface",
    chip: "surface",
    headline: "headline",
    title: "headline",
    claim: "headline",
    caption: "text",
    text: "text",
    label: "text",
    button: "button",
    target: "button",
    link: "button",
    logo: "logo",
    icon: "graphic",
    graphic: "graphic",
};
export const area = (b) => Math.max(0, b[2]) * Math.max(0, b[3]);
export const intersectArea = (a, b) => {
    const x = Math.max(a[0], b[0]);
    const y = Math.max(a[1], b[1]);
    return (Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - x) *
        Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - y));
};
export const intersect = (a, b) => {
    const x = Math.max(a[0], b[0]);
    const y = Math.max(a[1], b[1]);
    return [
        x,
        y,
        Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - x),
        Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - y),
    ];
};
/** Where an element is: what its containers show of it (a clip revealing a window counts), not cut by the canvas edge. */
export const centreOf = (s) => {
    const b = area(s.clipped) > 0 ? s.clipped : s.box;
    return { x: b[0] + b[2] / 2, y: b[1] + b[3] / 2 };
};
export const isAncestor = (a, b) => b.startsWith(`${a}/`);
export class Element {
    film;
    id;
    role;
    tagged;
    samples = new Map();
    /** Rules this element is decided to break, with why: `data-motion-ignore="rule: why; rule2: why"`. */
    ignores = new Map();
    constructor(film, 
    /** `role:name` when tagged, else an inferred id such as `surface#3`. */
    id, role, tagged) {
        this.film = film;
        this.id = id;
        this.role = role;
        this.tagged = tagged;
    }
    get frames() {
        return [...this.samples.keys()].sort((a, b) => a - b);
    }
    get first() {
        return this.frames[0];
    }
    get last() {
        const f = this.frames;
        return f[f.length - 1];
    }
    at(frame) {
        return this.samples.get(frame);
    }
    /** What it's called in a finding: its tag, or its text, or its kind and place. */
    get label() {
        if (this.tagged)
            return this.id;
        const s = this.samples.get(this.first);
        if (s.raw.x)
            return `${this.role} "${s.raw.x.slice(0, 28)}"`;
        const b = s.box;
        return `${this.role} ${Math.round(b[2])}x${Math.round(b[3])} at (${Math.round(b[0])}, ${Math.round(b[1])})`;
    }
    /** px moved from the previous frame, or 0 when it wasn't there. */
    step(frame) {
        const a = this.samples.get(frame - 1);
        const b = this.samples.get(frame);
        if (!a || !b)
            return 0;
        // Text that changes (typing, a counter) shifts its glyph box without moving.
        if (a.raw.x !== b.raw.x)
            return 0;
        const p = centreOf(a);
        const q = centreOf(b);
        return Math.hypot(q.x - p.x, q.y - p.y);
    }
    /** Rows inside it in a frame: its text, icons and children. */
    contents(frame) {
        const s = this.samples.get(frame);
        if (!s)
            return [];
        return this.film.rows(frame).filter((r) => isAncestor(s.raw.k, r.k));
    }
    /** Runs of motion separated by rest (under 45 px/s), each with its easing measured. */
    moves() {
        const fps = this.film.fps;
        const rest = 45 / fps;
        const frames = this.frames;
        const out = [];
        let start = -1;
        // A teleport isn't motion: it splits moves instead of joining them.
        const big = (40 * this.film.height) / 1080;
        const jump = (f) => isJump(this, f, big);
        // A single still frame between two runs of motion is a stall inside one move, not two moves.
        const bridge = Math.max(1, Math.round(fps / 30));
        const isMoving = (f) => this.samples.has(f) &&
            this.samples.has(f - 1) &&
            this.step(f) >= rest &&
            !jump(f);
        for (let i = 1; i <= frames.length; i += 1) {
            const f = frames[i];
            const next = Array.from({ length: bridge }, (_, k) => f + k + 1);
            const moving = i < frames.length &&
                f === frames[i - 1] + 1 &&
                (isMoving(f) || (start >= 0 && !jump(f) && next.some(isMoving)));
            if (moving && start < 0)
                start = i - 1;
            if (!moving && start >= 0) {
                const from = frames[start];
                const to = frames[i - 1];
                const steps = [];
                for (let g = from + 1; g <= to; g += 1)
                    steps.push(this.step(g));
                const distance = steps.reduce((a, b) => a + b, 0);
                const shares = easeShares(steps);
                const end = this.samples.get(to);
                const lands = this.film.onCanvas(end) && end.opacity > 0.3 && this.last >= to + 2;
                out.push({
                    from,
                    to,
                    steps,
                    distance,
                    seconds: steps.length / fps,
                    easeIn: shares.in,
                    easeOut: shares.out,
                    lands,
                });
                start = -1;
            }
        }
        return out;
    }
    ignored(rule) {
        return this.ignores.has(rule) || this.ignores.has("*");
    }
}
/**
 * A step far larger than the motion around it. The comparison skips the immediate
 * neighbours, so a teleport rendered across two frames (60 fps from 30 fps keys) still stands out.
 */
export const isJump = (el, f, min) => {
    const s = el.step(f);
    if (s <= min)
        return false;
    const around = Math.max(4, el.step(f - 2), el.step(f + 2), el.step(f - 3), el.step(f + 3));
    const pair = Math.max(el.step(f - 1), el.step(f + 1));
    return s > 4 * around && (pair < min || pair > 0.5 * s);
};
export const easeShares = (steps) => {
    const total = steps.reduce((a, b) => a + b, 0) || 1;
    let run = 0;
    let firstAt = steps.length;
    let lastAt = 0;
    steps.forEach((s, i) => {
        const before = run;
        run += s;
        if (before < 0.2 * total && run >= 0.2 * total)
            firstAt = Math.min(firstAt, i + 1);
        if (before < 0.8 * total && run >= 0.8 * total)
            lastAt = i;
    });
    return {
        in: firstAt / steps.length,
        out: (steps.length - lastAt) / steps.length,
    };
};
export class Film {
    composition;
    fps;
    width;
    height;
    durationInFrames;
    elements = [];
    byFrame = new Map();
    /** Which element each row belongs to, per frame. */
    owner = new Map();
    constructor(raw) {
        this.composition = raw.composition;
        this.fps = raw.fps;
        this.width = raw.width;
        this.height = raw.height;
        this.durationInFrames = raw.durationInFrames;
        for (const fr of raw.frames)
            this.byFrame.set(fr.f, fr.e);
        this.track();
    }
    get frames() {
        return [...this.byFrame.keys()].sort((a, b) => a - b);
    }
    get area() {
        return this.width * this.height;
    }
    rows(frame) {
        return this.byFrame.get(frame) ?? [];
    }
    /** The element a row belongs to in a frame. */
    elementOf(frame, key) {
        return this.owner.get(frame)?.get(key);
    }
    seconds(frame) {
        return frame / this.fps;
    }
    onCanvas(s) {
        const c = { x: s.box[0] + s.box[2] / 2, y: s.box[1] + s.box[3] / 2 };
        return c.x >= 0 && c.x <= this.width && c.y >= 0 && c.y <= this.height;
    }
    get(id) {
        const e = this.elements.find((x) => x.id === id);
        if (!e) {
            const tags = this.elements.filter((x) => x.tagged).map((x) => x.id);
            throw new Error(`No element tagged "${id}" in ${this.composition}. The tagged elements are: ${tags.join(", ") || 'none (tag elements with data-motion="role:name")'}`);
        }
        return e;
    }
    find(query = {}) {
        return this.elements.filter((e) => (!query.role || e.role === query.role) &&
            (!query.text ||
                e.frames.some((f) => {
                    const x = e.at(f).raw.x ?? "";
                    return typeof query.text === "string"
                        ? x.includes(query.text)
                        : query.text.test(x);
                })));
    }
    track() {
        const serials = new Map();
        let prev = new Map();
        let prevFrame = -Infinity;
        const tagged = new Map();
        for (const f of this.frames) {
            const rows = this.rows(f);
            const byKey = new Map(rows.map((r) => [r.k, r]));
            const owners = new Map();
            const groups = new Map();
            const tagCount = new Map();
            for (const r of rows) {
                if (r.id) {
                    const n = tagCount.get(r.id) ?? 0;
                    tagCount.set(r.id, n + 1);
                    const id = n ? `${r.id}#${n + 1}` : r.id;
                    let el = tagged.get(id);
                    if (!el) {
                        const role = ROLE_ALIASES[r.id.split(":")[0]] ?? r.id.split(":")[0];
                        el = new Element(this, id, role, true);
                        tagged.set(id, el);
                        this.elements.push(el);
                    }
                    this.add(el, f, r, owners);
                    continue;
                }
                // Text content isn't part of the shape, so a line typing out stays one element.
                const shape = r.k.replace(/#\d+/g, "");
                const g = groups.get(shape) ?? [];
                g.push(r);
                groups.set(shape, g);
            }
            const cur = new Map();
            for (const [shape, rs] of groups) {
                const cands = f === prevFrame + 1 ? (prev.get(shape) ?? []) : [];
                const centres = rs.map((r) => {
                    const b = area(r.v) > 0 ? r.v : r.b;
                    return { x: b[0] + b[2] / 2, y: b[1] + b[3] / 2 };
                });
                const assigned = new Array(rs.length);
                // One of this shape before and after, at the exact same node: the same element, however far
                // it moved (so a teleport shows as a jump instead of one element ending and another starting).
                if (rs.length === 1 && cands.length === 1 && cands[0].key === rs[0].k) {
                    assigned[0] = cands[0].el;
                }
                else {
                    const pairs = [];
                    // Among several of one shape: near, the same size and the same text is the same element.
                    const ratio = (a, b) => Math.abs(Math.log(Math.max(a, 1) / Math.max(b, 1)));
                    rs.forEach((r, i) => cands.forEach((c, j) => pairs.push([
                        Math.hypot(centres[i].x - c.centre.x, centres[i].y - c.centre.y) +
                            (r.x === c.text ? 0 : 60) +
                            150 * (ratio(r.b[2], c.size[0]) + ratio(r.b[3], c.size[1])),
                        i,
                        j,
                    ])));
                    pairs.sort((a, b) => a[0] - b[0]);
                    const usedC = new Set();
                    for (const [d, i, j] of pairs) {
                        // The same node with the same text is the same element however fast it moved.
                        const same = cands[j].key === rs[i].k && cands[j].text === rs[i].x;
                        if ((d > 250 && !same) || assigned[i] || usedC.has(j))
                            continue;
                        assigned[i] = cands[j].el;
                        usedC.add(j);
                    }
                }
                rs.forEach((r, i) => {
                    let el = assigned[i];
                    if (!el) {
                        const role = inferRole(r, this, byKey);
                        const n = (serials.get(role) ?? 0) + 1;
                        serials.set(role, n);
                        el = new Element(this, `${role}#${n}`, role, false);
                        this.elements.push(el);
                    }
                    this.add(el, f, r, owners);
                    const list = cur.get(shape) ?? [];
                    list.push({
                        el,
                        centre: centres[i],
                        text: r.x,
                        size: [r.b[2], r.b[3]],
                        key: r.k,
                    });
                    cur.set(shape, list);
                });
            }
            this.owner.set(f, owners);
            prev = cur;
            prevFrame = f;
        }
        this.settleRoles();
        this.promoteCursors();
    }
    add(el, frame, r, owners) {
        el.samples.set(frame, {
            frame,
            box: r.b,
            clipped: r.v,
            visible: intersect(r.v, [0, 0, this.width, this.height]),
            opacity: r.o,
            stretch: r.st ?? 1,
            raw: r,
        });
        owners.set(r.k, el);
        if (r.ig) {
            for (const part of r.ig.split(";")) {
                const [rule, ...why] = part.split(":");
                if (rule.trim())
                    el.ignores.set(rule.trim(), why.join(":").trim());
            }
        }
    }
    /** An untagged painted box is a surface if it's ever surface-sized, not only in its first frame (it may grow in). */
    settleRoles() {
        for (const el of this.elements) {
            if (el.tagged || (el.role !== "surface" && el.role !== "graphic"))
                continue;
            const first = el.at(el.first).raw;
            if (first.t !== "b")
                continue;
            const largest = Math.max(...el.frames.map((f) => area(el.at(f).visible)));
            el.role =
                largest > 0.02 * this.area && largest < 0.95 * this.area
                    ? "surface"
                    : "graphic";
        }
    }
    /** An untagged small graphic that travels far and squashes is a cursor. */
    promoteCursors() {
        // A glyph inside a tagged cursor is part of that cursor, never a cursor of its own.
        const tagged = this.elements.filter((e) => e.tagged && e.role === "cursor");
        const insideTagged = (el) => el.frames.some((f) => tagged.some((c) => c.at(f) && isAncestor(c.at(f).raw.k, el.at(f).raw.k)));
        for (const el of this.elements) {
            if (!el.tagged && insideTagged(el)) {
                el.role = "part";
                continue;
            }
            if (el.tagged || el.role !== "graphic")
                continue;
            const ss = [...el.samples.values()];
            const sizes = ss
                .map((s) => Math.max(s.box[2], s.box[3]))
                .sort((a, b) => a - b);
            const size = sizes[Math.floor(sizes.length / 2)];
            let travel = 0;
            for (const f of el.frames)
                travel += el.step(f);
            const stretches = ss.some((s) => s.stretch > 1.03);
            if (size >= 20 && size <= 320 && travel > 400 && stretches)
                el.role = "cursor";
        }
        let n = 0;
        for (const el of this.elements)
            if (!el.tagged && el.role === "cursor")
                el.id = `cursor#${(n += 1)}`;
        // A cursor's squash and stretch may be on any layer inside it: take the strongest.
        for (const c of this.elements.filter((e) => e.role === "cursor"))
            for (const f of c.frames) {
                const s = c.at(f);
                for (const r of this.rows(f))
                    if (r.st && isAncestor(s.raw.k, r.k))
                        s.stretch = Math.max(s.stretch, r.st);
            }
        // Text that travels with a cursor (its name label) is the cursor's, not a headline.
        const roots = new Map();
        for (const c of this.elements.filter((e) => e.role === "cursor"))
            for (const f of c.frames) {
                // A tagged cursor is its own root; an untagged one is a glyph inside its positioning wrapper.
                const k = c.at(f).raw.k;
                roots.set(f, [
                    ...(roots.get(f) ?? []),
                    c.tagged ? k : k.split("/").slice(0, -2).join("/"),
                ]);
            }
        for (const el of this.elements) {
            if (el.tagged || (el.role !== "headline" && el.role !== "text"))
                continue;
            const under = el.frames.filter((f) => (roots.get(f) ?? []).some((r) => isAncestor(r, el.at(f).raw.k)));
            if (under.length > el.frames.length / 2)
                el.role = "label";
        }
    }
    activityCache;
    /**
     * How much of the canvas changes into each frame: anything that moved, resized, faded,
     * changed its text, appeared or left (a move counts where it was and where it went, as a
     * viewer sees it). Children carried by a changing parent count once, as the parent.
     */
    activity() {
        if (this.activityCache)
            return this.activityCache;
        const out = new Map();
        const canvas = [0, 0, this.width, this.height];
        // Substantial change: a real move (faster than 45 px/s at 1080p), a resize of over 0.6% a frame,
        // a fade, new text, something arriving or leaving. A slow, steady move or zoom is drift: it
        // makes pixels change without giving the eye anything new, so it isn't activity.
        const fast = (45 * this.height) / 1080 / this.fps;
        for (const f of this.frames) {
            const changed = [];
            for (const el of this.elements) {
                const s = el.at(f);
                const p = el.at(f - 1);
                if (!s && !p)
                    continue;
                if (s && !p && f !== this.frames[0]) {
                    if (s.opacity > 0.05)
                        changed.push({
                            key: s.raw.k,
                            boxes: [s.visible],
                            kind: s.opacity > 0.3 ? "big" : "fade",
                            el,
                        });
                    continue;
                }
                if (!s && p) {
                    if (p.opacity > 0.05 && this.byFrame.has(f))
                        changed.push({
                            key: p.raw.k,
                            boxes: [p.visible],
                            kind: p.opacity > 0.3 ? "big" : "fade",
                            el,
                        });
                    continue;
                }
                if (!s || !p)
                    continue;
                // A zoom is judged by its rate, not by how fast a big surface's edges travel: a 3% push over
                // two seconds counts as slow drift however large the surface is.
                const shift = Math.hypot(s.box[0] + s.box[2] / 2 - p.box[0] - p.box[2] / 2, s.box[1] + s.box[3] / 2 - p.box[1] - p.box[3] / 2);
                const resize = Math.abs(s.box[2] - p.box[2]) / Math.max(p.box[2], 1) +
                    Math.abs(s.box[3] - p.box[3]) / Math.max(p.box[3], 1);
                const moved = shift > 0.1 ||
                    Math.abs(s.box[2] - p.box[2]) + Math.abs(s.box[3] - p.box[3]) > 0.2 ||
                    Math.abs(s.stretch - p.stretch) > 0.002;
                const fade = Math.abs(s.opacity - p.opacity);
                const retyped = s.raw.x !== p.raw.x;
                if (shift > fast ||
                    resize > 0.006 ||
                    fade > 0.03 ||
                    retyped ||
                    Math.abs(s.stretch - p.stretch) > 0.002)
                    changed.push({
                        key: s.raw.k,
                        boxes: moved ? [s.visible, p.visible] : [s.visible],
                        kind: "big",
                        el,
                    });
                else if (fade > 0.004)
                    changed.push({ key: s.raw.k, boxes: [s.visible], kind: "fade", el });
                else if (moved)
                    changed.push({ key: s.raw.k, boxes: [s.visible], kind: "drift", el });
            }
            const measure = (list) => {
                const keys = list.map((c) => c.key);
                const top = list.filter((c) => !keys.some((k) => k !== c.key && isAncestor(k, c.key)));
                const boxes = top
                    .flatMap((c) => c.boxes)
                    .map((b) => intersect(b, canvas))
                    .filter((b) => area(b) > 0);
                return {
                    share: Math.min(1, boxes.reduce((sum, b) => sum + area(b), 0) / this.area),
                    region: boxes.length ? union(boxes) : undefined,
                    elements: top.map((c) => c.el),
                };
            };
            // Something drifting inside something that really moves is carried, not drifting.
            const real = changed.filter((c) => c.kind !== "drift");
            const realKeys = real.map((c) => c.key);
            const drifting = changed.filter((c) => c.kind === "drift" && !realKeys.some((k) => isAncestor(k, c.key)));
            const all = measure(real);
            const big = measure(real.filter((c) => c.kind === "big"));
            const drift = measure(drifting);
            out.set(f, {
                share: all.share,
                region: all.region,
                bigShare: big.share,
                bigRegion: big.region,
                drift: drift.share > 0
                    ? { share: drift.share, elements: drift.elements }
                    : undefined,
                moved: new Set(real.filter((c) => c.kind === "big").map((c) => c.el)),
            });
        }
        this.activityCache = out;
        return out;
    }
    /** Blocks of headline type, linked across frames, with when they're fully shown. */
    headlines() {
        const out = [];
        const active = [];
        for (const f of this.frames) {
            const letters = this.find({ role: "headline" })
                .map((e) => e.at(f))
                .filter((s) => !!s && s.opacity > 0.05 && !!s.raw.x);
            const blocks = new Map();
            for (const s of letters) {
                const root = s.raw.k.split("/").slice(0, -2).join("/");
                blocks.set(root, [...(blocks.get(root) ?? []), s.raw]);
            }
            for (const rs of blocks.values()) {
                const text = readText(rs);
                const box = union(rs.map((r) => r.b));
                const inst = active.find((h) => f - h.last <= 2 &&
                    intersectArea(grow(h.box, 40), box) > 0 &&
                    overlapText(h.text, text));
                if (inst) {
                    inst.frames.set(f, rs);
                    inst.last = f;
                    inst.to = f;
                    if (rs.length >= (inst.frames.get(inst.from)?.length ?? 0)) {
                        inst.text = text;
                        inst.box = box;
                    }
                }
                else {
                    active.push({
                        from: f,
                        to: f,
                        last: f,
                        text,
                        box,
                        frames: new Map([[f, rs]]),
                    });
                }
            }
        }
        for (const h of active) {
            if (h.to - h.from < 6)
                continue;
            const max = Math.max(...[...h.frames.values()].map((rs) => rs.length));
            const full = [...h.frames.entries()]
                .filter(([, rs]) => rs.length === max && rs.every((r) => r.o > 0.95))
                .map(([f]) => f);
            const ref = full.length
                ? h.frames.get(full[Math.floor(full.length / 2)])
                : [...h.frames.values()].find((rs) => rs.length === max);
            out.push({
                from: h.from,
                to: h.to,
                full: full.length ? [full[0], full[full.length - 1]] : undefined,
                text: readText(ref),
                box: union(ref.map((r) => r.b)),
            });
        }
        return out.sort((a, b) => a.from - b.from);
    }
}
/** Read type made of separate spans (often one per letter or word) back into words, by position. */
export const readText = (rows) => {
    const sorted = [...rows].sort((a, b) => a.b[1] - b.b[1] || a.b[0] - b.b[0]);
    const lines = [];
    for (const r of sorted) {
        const line = lines.find((l) => Math.abs(l[0].b[1] + l[0].b[3] / 2 - (r.b[1] + r.b[3] / 2)) <
            r.b[3] / 2);
        if (line)
            line.push(r);
        else
            lines.push([r]);
    }
    return lines
        .map((l) => {
        l.sort((a, b) => a.b[0] - b.b[0]);
        let out = "";
        l.forEach((r, i) => {
            const prev = l[i - 1];
            if (prev && r.b[0] - (prev.b[0] + prev.b[2]) > 0.2 * (r.fs ?? 20))
                out += " ";
            out += (r.x ?? "").trim();
        });
        return out;
    })
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
};
export const union = (boxes) => {
    const x0 = Math.min(...boxes.map((b) => b[0]));
    const y0 = Math.min(...boxes.map((b) => b[1]));
    const x1 = Math.max(...boxes.map((b) => b[0] + b[2]));
    const y1 = Math.max(...boxes.map((b) => b[1] + b[3]));
    return [x0, y0, x1 - x0, y1 - y0];
};
const grow = (b, d) => [
    b[0] - d,
    b[1] - d,
    b[2] + 2 * d,
    b[3] + 2 * d,
];
const overlapText = (a, b) => {
    const count = (s) => {
        const m = new Map();
        for (const c of s.replace(/\s/g, ""))
            m.set(c, (m.get(c) ?? 0) + 1);
        return m;
    };
    const ca = count(a);
    const cb = count(b);
    let common = 0;
    for (const [c, n] of ca)
        common += Math.min(n, cb.get(c) ?? 0);
    const longest = Math.max(a.replace(/\s/g, "").length, b.replace(/\s/g, "").length);
    return common > 0 && common >= longest - 8;
};
/** Role of an untagged element from what it is: large free type, painted boxes, glyphs. */
const inferRole = (r, film, byKey) => {
    if (r.t === "t")
        return (r.fs ?? 0) >= film.height * 0.037 && !insideSurface(r, film, byKey)
            ? "headline"
            : "text";
    if (r.t === "g")
        return "graphic";
    const a = area(intersect(r.v, [0, 0, film.width, film.height]));
    return a > 0.02 * film.area && a < 0.95 * film.area ? "surface" : "graphic";
};
/** Product text lives inside a painted surface; headline type sits on the canvas. */
const insideSurface = (r, film, byKey) => {
    const parts = r.k.split("/");
    for (let i = parts.length - 1; i > 1; i -= 1) {
        const row = byKey.get(parts.slice(0, i).join("/"));
        const a = row && area(intersect(row.v, [0, 0, film.width, film.height]));
        if (row && row.t === "b" && a > 0.02 * film.area && a < 0.9 * film.area)
            return true;
    }
    return false;
};
