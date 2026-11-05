import { defineRule } from "../rule.js";
import { viewer } from "../viewer.js";
import { runs } from "./util.js";
const quote = (r) => `"${r.full.length > 40 ? `${r.full.slice(0, 38)}…` : r.full}"`;
/** Where the simulated viewer looked, other than at this text, while the text was on screen. */
const elsewhere = (film, r) => {
    const v = viewer(film);
    const time = new Map();
    for (let f = r.from; f <= r.to; f += 1) {
        const g = v.gaze.get(f);
        if (g && g.read !== r)
            time.set(g.label, (time.get(g.label) ?? 0) + 1);
    }
    return [...time]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 2)
        .map(([label]) => label);
};
export const unreadText = defineRule({
    id: "attention/unread-text",
    kind: "lint",
    severity: "error",
    options: { minSeconds: 0.4, share: 0.3 },
    docs: {
        summary: "A caption (large text that isn't inside a box with a background or border) stays on screen until the simulated viewer, reading 3.5 words a second, has read at least 70% of its words, and other text that the viewer starts reading stays until it has read at least 70% of it.",
        why: "A viewer reads about 3.5 words a second, and only where they are looking. Text that is on screen long enough on its own can still go unread when something else draws the eye while it's there, and when text leaves before it has been read, the film feels too fast. The rule follows each line of text from when it appears until it leaves and counts the words the simulated viewer read. A caption is text at least 32 px tall at 1080p, not in a code font, and never inside a box with a background or border (other than a backdrop the size of the canvas); all other text is treated as part of the product being shown, such as a window, card or panel. A caption is an error when at least 1 word and at least 30% of its words are unread; other text is a warning, and only when the viewer started reading it, it has at least 3 words, it didn't arrive as part of a block of more than 12 words (which the viewer skims), and at least 2 words and 30% of its words are unread. Text on screen for less than 0.4 s, or still on screen at the end of the film, is not checked.",
        fix: "Give the text time on its own, with nothing else new on screen while it is read: keep the product still, or let the action finish first. Otherwise use fewer words or show the text for longer. If the text doesn't need to be read, make it smaller or replace it with placeholder lines instead of words.",
        options: {
            minSeconds: "Text on screen for less time than this, in seconds, is not checked.",
            share: "The share of a text's words, from 0 to 1, that may go unread; a finding needs at least this share unread (and at least 1 word for a caption, 2 for other text).",
        },
    },
    check({ film, options, report }) {
        const v = viewer(film);
        const end = film.frames[film.frames.length - 1];
        for (const r of v.reads) {
            const missed = r.words - r.read;
            const caption = !r.surface;
            if (r.to >= end - 1 ||
                (r.to - r.from + 1) / film.fps < options.minSeconds)
                continue;
            // Captions must be read; product text only when the eye started on it and had to leave.
            if (caption ? r.words < 1.5 : r.read === 0 || r.scale < 1 || r.words < 3)
                continue;
            if (missed < Math.max(caption ? 1 : 2, options.share * r.words))
                continue;
            const away = elsewhere(film, r);
            report({
                element: [...r.elements][0],
                frames: [r.from, r.to],
                measured: `${quote(r)} leaves the screen after ${((r.to - r.from + 1) / film.fps).toFixed(1)} s with ${Math.round(missed)} of its ${Math.round(r.words)} words unread by the simulated viewer${away.length ? `, which was looking at ${away.join(" and ")} instead` : ""}`,
                need: `enough time on screen, without anything else new drawing the eye away, for the simulated viewer to read at least ${((1 - options.share) * 100).toFixed(0)}% of its words at 3.5 words a second`,
                severity: caption ? "error" : "warn",
            });
        }
    },
});
export const competing = defineRule({
    id: "attention/competing",
    kind: "lint",
    severity: "error",
    options: { warnAbove: 0.15, errorAbove: 0.25, place: 0.15, seconds: 0.4 },
    docs: {
        summary: "In at most 15% of the film's frames are there two unread texts, more than 15% of the canvas width apart, that are both waiting to be read (a warning above 15% of frames, an error above 25%).",
        why: "A viewer reads one place at a time. When a caption appears while a message types out elsewhere, the viewer can't read either properly and feels rushed. Using the simulated viewer, a text is waiting to be read when it appeared or changed in the last 1.5 s, has at least 2 words, and hasn't been read yet. In the films we measured, the ones people approved had two such texts in different places about 10% of the time, and the ones people found overwhelming had them 18% to 36% of the time. When the film is over the limit, each stretch of 0.4 s or longer where this happens is also reported as a warning naming the two texts.",
        fix: "Show the texts one after the other: let the viewer finish one (at 3.5 words a second) before the next appears or changes, or show one idea as an action on screen instead of in words. Two short texts can be on screen together if one of them has already been read.",
        options: {
            warnAbove: "Share of the film's frames, from 0 to 1, with two texts waiting to be read in different places, above which the rule reports a warning.",
            errorAbove: "Share of the film's frames, from 0 to 1, above which the finding is an error instead of a warning.",
            place: "How far apart two texts' centres must be, as a share of the canvas width from 0 to 1, to count as being in different places.",
            seconds: "Shortest stretch, in seconds, of two texts waiting to be read at once that gets its own finding naming them (only when the film is over warnAbove).",
        },
    },
    check({ film, options, report }) {
        const v = viewer(film);
        const place = options.place * film.width;
        const both = [];
        const pairs = new Map();
        for (const f of film.frames) {
            const asking = v.asking.get(f) ?? [];
            const centre = (r) => [
                r.box[0] + r.box[2] / 2,
                r.box[1] + r.box[3] / 2,
            ];
            for (let i = 0; i < asking.length; i += 1)
                for (let j = i + 1; j < asking.length; j += 1) {
                    const [a, b] = [centre(asking[i]), centre(asking[j])];
                    if (Math.hypot(a[0] - b[0], a[1] - b[1]) > place && !pairs.has(f)) {
                        both.push(f);
                        pairs.set(f, [asking[i], asking[j]]);
                    }
                }
        }
        const share = both.length / film.frames.length;
        if (share <= options.warnAbove)
            return;
        const spans = runs(both, 2).filter(([a, b]) => (b - a + 1) / film.fps >= options.seconds);
        report({
            frames: [film.frames[0], film.frames[film.frames.length - 1]],
            measured: `Two texts in different places are waiting to be read at the same time in ${(share * 100).toFixed(0)}% of the film's frames (${spans.length} stretches long enough to be reported on their own)`,
            need: `two texts in different places waiting to be read at once in at most ${(options.warnAbove * 100).toFixed(0)}% of the film's frames`,
            severity: share > options.errorAbove ? "error" : "warn",
        });
        for (const [a, b] of spans) {
            const [x, y] = pairs.get(a);
            report({
                element: [...x.elements][0],
                frames: [a, b],
                measured: `${quote(x)} and ${quote(y)}, in different places, are both waiting to be read for ${((b - a + 1) / film.fps).toFixed(1)} s`,
                need: "the viewer can finish reading one text before the other appears or changes",
                severity: "warn",
            });
        }
    },
});
export const attentionRules = [unreadText, competing];
