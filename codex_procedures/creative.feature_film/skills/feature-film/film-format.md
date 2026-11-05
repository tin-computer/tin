# film.json

A film is one JSON object. `tin-film inspect` writes a starter; keep its `site.url` and `now`.

```json
{
  "title": "Draw a circle, and the sheep follow",
  "story": {
    "before": "Herding games make you steer one dog at a time.",
    "after": "In Sheepdogs you draw a circle round the sheep and they follow it."
  },
  "site": {"url": "https://example.com/"},
  "now": "2026-11-05T10:00:00Z",
  "format": "16:9",
  "theme": "dark",
  "ready": "h1",
  "prepare": [
    {"click": "#cookie-accept"}
  ],
  "beats": [
    {"camera": "body", "cut": true, "pad": 0},
    {"caption": "Draw a circle round the sheep, and they follow it."},
    {"begin": true},
    {"hold": 2.5},
    {"mark": "poster"},
    {"caption": "How it works is on the blog."},
    {"click": "a[href=\"/blog/how-herding-became-drawing\"]"},
    {"ready": "h1"},
    {"hold": 1.5},
    {"scroll": ["html", "#how-it-works"], "duration": 1.2},
    {"hold": 2.5},
    {"mark": "how"},
    {"end": true}
  ],
  "notes": ["The game's sheep move on their own, so their frames differ slightly between takes."]
}
```

## Fields

- `title`: the note's title and the film's name. A sentence about the change, at most 80
  characters.
- `story`: `before` and `after`, one sentence each. The note opens with them.
- `site.url`: the https page the film opens on. `site.reads`: rare; a list of address patterns
  (regular expressions) for requests that are not GETs but only read, such as a search that
  posts its query. A request that sends, publishes or pays never goes in it.
- `now`: the time the page's clock shows in every take. Keep the starter's.
- `format`: `16:9` (LinkedIn, X, the site and blogs; the site is filmed at desktop width), `4:5`
  or `1:1` (feeds seen on phones; the site is filmed at its narrower layout). Use the
  run's `format`.
- `theme`: `dark` puts the window on black with white captions; `light` on white with dark ones.
  Pick the one closest to the site's own ground.
- `ready`: a selector the first page shows once it has loaded.
- `prepare`: beats played before the film starts, unfilmed: dismiss a cookie banner or close a
  popup with a `click`. Nothing else goes here.
- `beats`: the film, in order.
- `notes`: sentences for the note's checks section, one per finding you left and why.

## Beats

A beat's first key is its action; the other keys are its options. A selector is CSS; take it from
`tin-film inspect`. A selector that matches several elements frames all of them.

| Beat | What it does |
| --- | --- |
| `{"camera": "body", "cut": true, "pad": 0}` | Frame a target. The first beat frames the whole page with a cut. Options: `pad` (CSS px round it, 28), `duration` (s, 1.1), `cut` (no move), `align` and `valign` (`top`, `center`, `left`, `right`), `text` (frame only its words). |
| `{"caption": "…"}` | Change the caption under the picture. One at a time; it stays until the next. |
| `{"begin": true}` | Fade in. Once, after the first camera and caption. |
| `{"hold": 2}` | Let seconds pass, filmed. |
| `{"move": "selector"}` | The hand travels to a target. `to: [x, y]` is where in it, as fractions (0.5, 0.5). |
| `{"click": "selector"}` | Move there and press. A link that opens a page navigates the window: `ready` on the new page next. |
| `{"type": "words"}` | Type into what has focus (click the field first), a key at a time. |
| `{"ready": "selector"}` | Film until it is visible. `within` (s, 10). |
| `{"scroll": ["html", "selector"], "duration": 1.2}` | Scroll a container (`html` for the page) until the target sits `offset` px (24) below its top. |
| `{"highlight": "selector"}` | One soft mark over what just changed. `hold` (s, 1.1). |
| `{"follow": {"zoom": 1.6}}` | A camera that keeps the hand in frame; `{"still": true}` ends it. Use only for a long run of clicks in one area. |
| `{"mark": "poster"}` | Keep this frame as a still. `poster` is required. |
| `{"end": true}` | Fade out. The last beat. |

`"when": "wide"` or `"when": "tall"` plays a beat only in 16:9 or only in 4:5 and 1:1.
`"if": "selector"` plays it only while that shows, such as a banner that isn't always there.
