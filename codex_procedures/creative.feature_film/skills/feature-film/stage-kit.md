# The stage kit

A stage is one HTML page, `stage/index.html` beside `film.json`. It links the kit's stylesheet
and runtime, sets the product's look, places its parts, and leaves every part that arrives later
hidden until a beat shows it:

```html
<link rel="stylesheet" href="/film-fonts/fonts.css">
<link rel="stylesheet" href="/__stage/kit/stage.css">
<style>
  :root { /* the product's tokens, from tin-film inspect */ }
  #terminal { left: 48px; top: 40px; width: 780px; height: 520px; }
</style>
<div class="stage"> … the parts … </div>
<script src="/__stage/kit/stage.js"></script>
```

Files beside `index.html` (an image, a font) load by their name. The worked examples
(`tin-film new --from onboarding` or `people`) are the reference for everything below: copy the
closest one's structure, change its look and its words.

## The screen

A 16:9 film plays a screen of 1080 × 517 CSS px (`"screens": {"16:9": 1080}` in `film.json`),
shown at 1.78 times its size: 15px text reads as 27px. 4:5 and 1:1 films play 720 × 760 and
720 × 600. Design at those sizes; never shrink type to fit more in.

## Framing

- One gutter, 48px at 16:9: the hero sits a gutter from the top and left edges and runs off the
  right or the foot of the stage, never floating whole with air on four sides.
- One hero per scene: a window, the product's app, a terminal. At most one satellite: a card that
  carries the shot's point, straddling the hero's edge about half outside, over the hero's quiet
  part, never over its words.
- A satellite present from the first frame fills its column; a column left empty for seconds is a
  void. Change a card into the next in place (`swap`) rather than emptying the column.
- Nothing the story touches or says is cut by the stage's edge. Only filler runs off: empty days,
  the rest of a list, the right of a window.
- Real words only on what the story is about, three to seven strings a frame; quiet bars
  (`<i class="bar">`) for the rest. The product's chrome (its nav, its title, its tabs) keeps its
  real words: that is how a viewer knows the product.
- A result sits in the hero's quiet margin, a gutter from the edges, never over its controls.

## Parts

Each is plain HTML with the kit's classes; restyle with the tokens, not new geometry.

- Window: `.window` with `.window-bar` (three `<i>` and a `.window-title`, or a
  `.window-address` for a browser) and a `.window-body`. Place it with `left`, `top`, `width`,
  `height` on its id.
- Terminal: `.window.terminal`; its `.window-body` holds a `.scroll` with the prompt line and an
  empty box (`#term`) that `print` fills. Lines are pre-formatted: `.muted`, `.soft`, `.ok`,
  `.claude`, `<b>`, `.gap`, `.rule`. A picker row is `.option` with `.mark` and
  `<span class="box"><i>✔</i></span>`; `tick` turns it on.
- Card: `.card` with `.card-head` (`.card-title`, `.card-sub`), `.card-body` and `.card-foot`.
  Rows inside: `.row` (`.label`, `.value`) and `.rank` (`.n`, `.what` with `.name-row`, `.name`,
  `.detail` or a `.bar`; `.is-top` for the first). A card that becomes the next is `.card.sized`
  with direct children `data-view="…"`, the first `.is-current`.
- App: `.window.app`: `.app-side` (`.app-mark`, `.app-project`, `.nav` items with an `svg`, a
  `span` and a `.count`) and `.app-main` holding pages `data-page="…"`, the first `.is-current`.
  A nav item with `data-go="page"` opens that page when the film clicks it.
- Calendar: `.tabs` with `.tab`; `.week` of `.day` (`.day-name`, `.is-today`) holding `.event`
  cards (a title, a `.foot` with a time and a `.state`).
- Running work: `.group-title` and `.run` with a `.dot`, `.name`, `.state` and a `.track i` bar.
- Decision: `.decision` with `.decision-head`, `.decision-body` and `.decision-actions`.
- Button: `.button` (`.primary`, `.quiet`) with its words in `.label`. `data-then="Working|Done"`
  makes a click change its label in steps on itself, and `data-show="#id"` brings in a part after.
- Table: `.table` with its columns in `--cols` (a grid template), a `.thead` part with
  `data-view="columns"` (the column names, `.is-sorted` on one) and, when rows can be ticked,
  `data-view="selected"` with class `.picked` (a `.tickbox.is-mixed`, the count in `<b>`, a
  `.line` and a `.button.go`); then `.tr` rows: a `.tickbox` with `data-tick="#row-id"`, the
  name in `.who`, `.sub` (or a `.bar`), `.num` (`.is-fit` for a value that matters) and
  `.status` (`.queued`, `.connected`). A ticked row (`tick`) tints and its box fills.
- Popover: `.pop` with a `<p>` and a `<code>` line, placed under the value it explains. Small
  enough to fade in over the page.
- Side panel: `.panel` with `.panel-head` (`.panel-title`, `.panel-sub`, a `.link` with an
  `.out` arrow), `.field` rows (`.label`, then `<b>` and a `<p>`; `tick` lights one) and
  `.happened` lines (what happened, then its time in `<code>`). Put it on the stage over the
  window, not inside it, as `.arrive.slide.from-right`, running off the stage's right edge like
  the window.
- Social post: `.card.post` with `.post-head` (an `.avatar`, `.post-name`, `.post-sub`),
  `.post-text`, quiet `<i class="bar">` lines, `.post-media` and `.post-foot` (`.faces` and the
  counts). People nobody can read yet: `.face-row` (an `<i class="face">` and two bars).
- Chip `.chip`, dot `.dot` (`.ink`, `.hot`, `.pulse`), toast `.toast`, pull request `.pull`.
- A diagram (an iceberg, rings, a stream, a field of dots) is built from boxes on the stage, not
  kit parts; see `directions.md`. Wrap one that sits whole in the frame in `data-diagram`.

Arrivals: `.arrive` (hidden until shown; it fades and rises in), `.arrive.slide` (comes up from
below the stage, opaque: a window or a result), `.arrive.slide.from-right` (comes in through
the stage's right edge, opaque: a side panel), `.arrive.grow` (opens its height in a column,
carrying what is below it; wrap a padded card in it).

## The runtime

A beat calls the stage with `{"do": ["name", …arguments]}`. Each call schedules its motion on the
film's clock and the film holds for as long as it takes.

| Call | What it does |
| --- | --- |
| `["show", "#a, #b .row", {"stagger": 0.1}]` | Bring in `.arrive` parts, in document order. |
| `["hide", "#a"]` | Fade a part out. |
| `["type", "#cmd", "words", {"cps": 20}]` | Type into an element, with a caret. |
| `["print", "#term", ["line", "line"], {"gap": 0.25}]` | Add lines to a terminal or list; a full terminal scrolls by whole lines. |
| `["set", "#v-goal", "More signups"]` | Change words in place: the old lift away, the new rise in; a `.sized` part eases to its new size. |
| `["tick", "#opt1"]` | Turn on a checkbox, picker row or chip. |
| `["swap", "#card", "plan"]` | Show a card's next view in place. |
| `["away", "#terminal, #card"]` | The scene travels up and off; pair it with `show` of an `.arrive.slide` window to bring the next one up. |
| `["fill", "#bar", 0.4, {"seconds": 1.4}]` | Grow a bar to a share. |
| `["page", "#app", "decisions"]` | Switch a window's page without a click. |
| `["together", [["hide", "#a .tickbox"], ["set", "#a .status", "Queued"]]]` | Several calls as one beat; it takes as long as the longest. |

Clicks are the film's own (`{"click": "#nav-decisions"}`); the stage answers them as the product
would through `data-go`, `data-then` and `data-tick`. Never draw a cursor in the stage.

## Motion

- One thing happens at a time: a line prints, then a card answers, then the hand moves.
- Nothing pops: parts arrive, words roll, sizes ease. A big surface never fades over another.
- Pulsing dots never settle; leave `.pulse` off anything on screen at a mark.
- The hand moves only to parts already on stage, and rests on empty surface before a mark.
- Hold the poster and the payoff still for a second or more, the result in view.
