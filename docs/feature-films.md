# Feature films

A feature film is a short, silent, captioned video of one thing Tin does, made for a LinkedIn or
X post about what shipped. It is filmed from Tin's own dashboard (the packaged UI, the same code
that ships) running on a demo world, one frame at a time, with a directed camera, cursor and
captions. Filming it again gives the same film frame for frame, so a film can be remade whenever
the feature changes.

```
npm run film -- people-requests                     # 4:5 and 16:9
npm run film -- people-requests --format 16:9 --theme light
TIN_FILM_FONTS=https://fonts.tin.computer/v1/private-fonts.css npm run film -- people-requests
```

It needs Playwright's Chromium (`npx playwright install chromium`) and `ffmpeg` on the path.
Each format lands in `.films/<film>/<format>/`:

| File | What it is |
| --- | --- |
| `film.mp4` | H.264, yuv420p, fast start, 30 fps, no audio track |
| `film.gif` | 15 fps, palette per film, under X's 15 MB |
| `poster.png` | The frame the script marks `poster`, at full size |
| `marks/`, `sheet.png` | Every marked frame, and a contact sheet of them for review (a rehearsal's are half-size JPEGs) |
| `motion/` | Every camera move, click and cut as a strip of every other frame (66ms apart), for reviewing the motion itself |
| `captions.srt`, `captions.vtt` | The captions with their times, for platforms that take a caption file and for the browser's video track |
| `report.json` | Frames, length, sizes, frame hashes and the checks below |

## Formats

| Format | Size | Screen the dashboard is filmed on | Use |
| --- | --- | --- | --- |
| `4:5` | 1080 × 1350 | 720 px wide, so People and the cards use their compact layouts | LinkedIn and X feeds |
| `16:9` | 1920 × 1080 | 1440 px wide, the desktop layout | X on desktop, the site, blog posts |
| `1:1` | 1080 × 1080 | 720 px wide | where a square fits better |

The dashboard runs in its own window: an iframe exactly the screen's size, on a stage page that
draws everything else. Inside it the dashboard lays out, places its popovers and answers its
breakpoints as in any browser. The stage holds the backdrop, black under the dark theme and white
under the light one, with the caption band and the fades in the same colour. A film picks its
screen per format (`screens`). The stage is a band's height taller than the room the camera may
frame, so the band never covers the dashboard.

A screen with a `margin` (`{width, height, margin}`, a number or `{top, right, bottom, left}`)
sits in from the stage's corner as a window, as in Screen Studio. Its corners are rounded where
both sides sit on the backdrop, it has a hairline edge, and on white a short soft shadow. The
margin is room the camera may frame past the window's edge.

There are two ways to frame a film, as in Screen Studio:

- **Still.** The camera moves only when the script says (`f.camera`), at most one push-in. The
  People film's 4:5 cut works this way: the screen fills the picture's width and its top menu
  stays above the frame.
- **Follow.** The film opens on the whole window, centred with the same padding on every side.
  From `f.follow()` the camera eases in to one zoom and keeps centring on the cursor, plus what
  the film names (`attention`, such as a toast). A film knows where the hand is going, so on a
  trip the camera aims at where it will land, never chasing it out and back. `f.look(targets)`
  frames a beat before it happens, such as a row and where its panel will open, so the camera
  moves once; `f.look(null)` lets go. Marks pull the frame towards them when they fit beside
  the cursor. It re-aims as an operator does: to bring something into view, never because
  something left. That means when the hand's landing point leaves the middle of the frame, when
  something it must show leaves the frame, or when a new mark appears outside it. It only
  re-aims to a frame that is really different. Each move is a glide, 0.7 to 1.2 s by distance,
  that sets off at the camera's own speed and ends exactly still. Between moves it holds still,
  never creeping or nudging. It opens out only as far as what must show needs.
  Near the window's edge it keeps centring, up to `overshoot` (30%) of the frame past the edge,
  where the window's boundary shows on the backdrop. Size the room's margin for that. The People
  film's 16:9 cut follows.

Framing follows Stageshow's `references/framing.md`:

- **Hard edges.** Nothing fades into the backdrop. The window's edges and the picture's crops
  are clean lines.
- **Symmetry at rest.** A whole window sits centred with even padding. A film's still framing
  shows one gutter, a full one or well past, on every edge of the window that shows.
- **Crop only filler.** In a still framing, no word, field or button is cut by the picture's
  edge. The camera nudges each framing (up to `nudge`, 24 px by default, never above `floor`) to
  a clean line. A following camera crops as it goes, as Screen Studio's does. End a window just
  under its last row's words, so a toast near its foot covers whole rows.
- **What opens stays in frame.** A toast, a card or a panel the story opens shows whole. A
  following camera is told about it with `attention`.

## How it works

- **The demo world.** `serveApp` from `web/app-fixture.js` serves the dashboard with synthetic
  APIs, given a film's own people, connections and numbers (`web/film/world.js` builds them).
  There is no database and no account. Every request to anywhere else is blocked and reported,
  and the server's clock is the film's day, so what it stamps reads as recent.
- **The clock.** `web/film/clock.js` replaces the page's timers, animation frames, CSS animations
  and `Date` before the dashboard loads. Time moves only when the recorder steps it, by 1/30 s.
  Every request lands in the frame that made it, so two takes are identical.
- **The director.** The script calls `f.camera`, `f.move`, `f.click` and so on. Each frame the
  recorder moves the camera and cursor and steps the dashboard's clock. It then draws the marks
  inside the dashboard's window and the stage's overlay (cursor, ring, fade, caption band), and
  screenshots the stage. The stage page is the shot itself, at the output's size, and the camera
  is a CSS transform on the room inside it, as Stageshow's Camera is. Motion is therefore smooth
  to a fraction of a pixel, text is drawn straight at the output's resolution, and the caption
  band at the foot holds still whatever the camera does.
  Clicks, hovers and typing are real input to the real dashboard. `f.app` is the dashboard's
  frame, for anything a script does in it directly; `f.page` is the stage.
- **Fonts.** Public builds film in Geist. `--fonts` (or `TIN_FILM_FONTS`) names a licensed
  stylesheet; the recorder fetches it and its files and serves them from the film's own origin,
  where `index.html` asks for the private stylesheet. Nothing licensed is committed.

## Writing a film

A film is a module in `web/film/films/` exporting `now` (the film's day), `timezone`, `world()`
and its `beats`: the director's calls as data (`web/film/spec.js`). `people-requests.js` is the
example; a desk film gives each scene its own `beats`. A film may still export `play(f)` instead.

```js
export const beats = [
  {camera: "@column", cut: true, pad: 0, valign: "top", when: "tall"},
  {caption: "Every score comes with its reason."},
  {move: '.people-table tr[data-person="p-daniel"] td.is-fit', to: [0.95, 0.12]},
  {hold: 2},
  {open: "/people", ready: ".people-table"},
  {highlight: "@linkedinRow", hold: 3.5, pad: 0, over: true, if: ACCEPTED},
  {morph: [{run: "chloeAccepts"}, {hold: 0.3}], caption: "They accept."},
  {mark: "poster"},
];
```

- **A beat** is one call. Its first key is the action, named as the method below
  (`term.paste` and the other `term.*` drive a desk's terminal). The action's value is the first
  argument, `true` for none. Every other key is an option, even one named like an action, as
  `ready` is in `{open: "/people", ready: …}`. For `wait`, `show`, `scroll` and `term.work`, the
  value is the list of arguments.
- **`when`** plays a beat only in some takes: `wide` or `tall` (the format), `full` or `brisk`
  (a desk scene's pace). **`if`** plays it only while a selector shows. `{when, beats: […]}` groups
  beats, and `{morph: […], caption}` plays them as one change shown in place.
- **`values`** are the film's named values, written `"@name"` anywhere in a beat: a place on the
  desk, a terminal's lines, or a function that measures the page when the beat plays (a panel
  row, the content column). `{run: "name"}` calls one for what no action covers, such as moving
  the world on or naming an element so later beats can point at it.
- The beats are plain JSON, and the recorder checks every one before it films: an unknown action,
  a `when` that names no take, or a missing value stops the film before its first frame, naming
  the beat.

| Call | What it does |
| --- | --- |
| `f.camera(target, {pad, duration, curve, cut, text, inside, nudge, floor})` | Frames the target (a selector, a list of selectors, or a rect). `text` frames only the words inside, `inside` keeps the frame within a region (`f.inside` sets a default). 1.1 s on Stageshow's camera curve by default (`house` for a slow, longer move); never zooms past the device scale, and moves up to `nudge` px so no edge cuts a word. |
| `f.move(target, {to})` | The cursor travels in a shallow arc, on a minimum-jerk reach (Flash and Hogan, 1985), so its speed rises and falls as one smooth bell, as a person's pointing does, timed by distance. The page sees the pointer only once the hand is over its target: nothing it passes over lights up. |
| `f.click(target)` | Moves, rests, presses: the cursor dips to 0.9 in 80 ms and lets go over 260 ms, and the page gets a real click. A film exporting `ripple = true` also sends Stageshow's soft ring out, for when clicks must read at a glance; it fades early if what was pressed goes. |
| `f.type(text)`, `f.scroll(container, target)` | Real keystrokes; an eased scroll of a container. |
| `f.caption(text)` | One caption at a time; the last fades out and the next rises in. `null` clears it. |
| `f.hold(seconds)`, `f.ready(selector)` | Lets time pass; waits (filmed) until something is visible. |
| `f.morph(change, {caption})` | Shows a change where it happens, with no cut. `change` moves the world on and has the app re-render, unfilmed; then what differs on screen moves, after Stageshow's motion rules. A word that changed rolls (the old slides up and out, the new rises in, its span easing to its new width). Rows that moved slide to their new place once what left has faded. New rows fade in with a small rise. Text that went fades where it was. The film's clock moves it all frame by frame. |
| `f.open(route)`, `f.cut(change, {caption})` | Opens a view; fades to the ground into the cut and settles out of it, doing `change` unfilmed (open a view, move the world on, let time pass); `caption` arrives with the new scene. |
| `f.highlight(target, {hold, delay, pad, over})` | A soft mark on what just changed: it washes in, holds and fades. It is drawn in the page, under its toasts and inside its fades; `over` puts it above a panel. |
| `f.begin()`, `f.end()` | Fades in from the band's ground (from 85%, so the first frame isn't dark) and out to it. |
| `f.shows(selector)` | Whether the target is on screen and on top: not scrolled out of its panel or covered. |
| `f.mark(name)` | Keeps the frame just shot for the sheet. `poster` is required. |
| `f.follow({zoom, attention, overshoot})`, `f.still()`, `f.look(targets)` | Starts and stops the following camera, and tells it what a beat will need in frame (above). |
| `f.app`, `f.page` | The dashboard's frame and the stage, for anything the director doesn't cover. |
| `f.world`, `f.wide` | The served world, to move it on behind a cut; whether the format is wide. |

The cursor is Stageshow's: macOS's arrow and pointing hand at its large size (1.5x), drawn in the
page's own pixels with a soft shadow, so it keeps its proportion to the interface and grows with
the camera. It is the hand over a link, button or box the page points at and the arrow everywhere
else, a clickable row included, and a change of kind crosses over in 0.2 s.

How a good film reads, after Stageshow's guidance (its `references/motion.md` and `framing.md`):

- one feature, three to five beats and a payoff, about 15 to 20 seconds;
- open on a finished, readable screen, and hold one framing that works as a still: the camera
  needs a reason, at most one push-in (or pull-out) per film, slow, within 2x, with the cursor
  still while the view moves;
- the cursor moves only with a cause, aims inside its target but off its words, and rests on
  empty surface between beats;
- show a change where it happens (`f.morph`), as the product would on its next refresh; keep a
  cut for a real change of scene. A cut changes only what time changed, so nothing from the last
  scene (a toast, a hover) survives it;
- one short caption per beat, in sentence case; feeds play muted, so the captions carry it;
- the poster is the decision moment, the frame that tells the story alone;
- everyone and everything in the demo world is made up.

## A live site, from its recording

A film can film any site instead of Tin's dashboard: its `world()` returns
`{site: {url, recording}, ready}`, and its screen opens the site. The first take with
`--record on` plays the live site and saves every answer its pages ask for in
`.films/worlds/<recording>/` (`index.json` and `bodies/`, out of git); every take after that is
played from the recording alone (`web/film/recording.js`). `films/sheepdogs-site.js` films
sheepdogs.io, one of Tin's test projects, as beats alone.

```
npm run film -- sheepdogs-site --format 16:9 --record on    # once, against the live site
npm run film -- sheepdogs-site --format 16:9,4:5 --check on # from the recording
```

- **Strict replay.** A request the recording doesn't hold is refused, and the take fails naming
  it, so a gap never reaches the screen as an empty pane. Record again to fill it.
- **No outside effect, live.** While recording, a request other than a GET goes out only when the
  film names it as a read (`site.reads`, patterns such as a GraphQL endpoint). Anything else, a
  send, a publish, a payment, is stopped and named, and the take fails until the film stages its
  answer (`site.answer(method, url, body)`). Trackers and Tin Analytics' `/ingest/` reporter get an
  empty answer.
- **No secrets.** Request headers are never saved, and a response's cookies and framing rules are
  dropped. `site.secrets` (a test account's password, a token) and the cookie values of
  `site.session`, a signed-in browser's storage state for a site behind sign-in, are looked for in
  every saved answer, and the take fails naming the answer that holds one.
- **Workers.** The film's clock runs the page's own timers and frames; a game or tool that
  simulates in a Web Worker runs on real time there, so its frames differ between takes (the
  Sheepdogs game does; its blog post, filmed next, matches frame for frame).

## Staged films

A staged film plays a scene built for it instead of a product or a site: one HTML page of
photogenic parts beside `film.json`, in the product's own look, simplified to what the viewer
needs, after [Stageshow](https://github.com/benjitaylor/skills) (MIT). It is what the
`creative.feature_film` workflow makes: a feature shown the way a launch page shows it, where one
thing happens per beat and the result stays on screen, rather than a camera over a landing page.

- **The kit.** `web/film/stage/stage.css` holds the parts (windows, a terminal, cards, rows and
  ranks, an app with its rail and pages, a calendar, running work, a decision, buttons, a pull
  request, a table with boxes to tick, a popover, a side panel, a social post) and the arrivals
  (`.arrive`, `.slide`, `.slide.from-right`, `.grow`); its tokens default to Tin's dark theme and
  a stage sets them to its product's. `web/film/stage/stage.js` is the runtime the beats call:
  `{"do": ["print", "#term", [...]]}` shows, prints, types, sets, ticks, swaps, fills or sends a
  scene away on the film's clock (`together` runs several at once), and the film holds while it
  plays. The film's own clicks reach the stage, which answers through `data-go` (a page of its
  window), `data-then` (a button that changes its label on itself) and `data-tick` (a box).
- **Diagrams.** The kit carries D3 7.9.0, d3-sankey 0.12.3 and Observable Plot 0.6.17
  (`web/film/stage/vendor/`, unmodified builds; see THIRD_PARTY_NOTICES), so a stage can lay out
  data the way a notebook does. The film clock drives D3's transitions like everything else; the
  runtime adds `draw` (paths draw themselves) and `count` (numbers count up). Inside a
  `data-diagram` element the probe measures each SVG mark (by its id, with its stroke), so the
  checks see a diagram's motion, and a diagram may sit whole in the frame. Each still checks that
  a diagram's marks centre on the caption's centre line and the picture's middle (within 1% and
  4%) and that its words keep a gutter from the edge.
- **Serving.** `"stage": "stage/index.html"` in `film.json` opens that page in the film's window;
  the page reaches its own folder (`/__stage/…`), the kit (`/__stage/kit/…`), the dashboard's
  assets (`/assets/…`) and the licensed fonts, and nothing else unless it was recorded.
- **Scale.** A 16:9 stage is a 1080 × 517 screen (`"screens": {"16:9": 1080}`), shown at 1.78
  times its size, so 15px text reads as 27px on a phone; the camera stays on the whole stage.
- **Storyboards.** A staged film keeps `storyboard.md` beside `film.json`: its title, the shape of
  feature it suits, the story, its directions (five far-apart visual ideas from different families
  and the one picked, per the skill's `directions.md`), then a `## N. Name` section per shot with its mark, caption, hero,
  satellite, real words and what moves. The film marks every shot, so a take's contact sheet is
  the storyboard drawn, one still per shot; every take of a staged film says where the film and
  its storyboard disagree (a shot's mark or caption missing, marks in another order, no poster).
  It also warns when a mark's hero (its largest painted part) sits whole on the stage with air
  on every side, when the stage draws a cursor of its own beside the film's hand, and when its
  text is set in a system face or in one that never loaded; an eyebrow (a small uppercase,
  letter-spaced or mono label above a title, a card's words or a number) is an error.
- **The product's type and theme.** `tin-film inspect` reads the faces the page's visible text
  is set in (not the body's), keeps their files from its own recording as `look/fonts.css` and
  `look/fonts/`, and reads the page's dark theme too, from a load that asks for one, so a stage
  is set in the product's own type on its own ground.
- **The worked examples.** `web/film/examples/` holds the bar the workflow copies (`tin-film new
  --from <name>`) and rebuilds for each product, one per shape of feature. Each keeps the contact
  sheet of its rehearsal as `sheet.jpg`, which `new` copies as `example.jpg`: after changing an
  example, rehearse it and copy the new sheet beside it.
  - `onboarding`: Tin's own onboarding, from "Set up Tin" in Claude Code to a pull request opened
    from Decisions, 34 seconds: a terminal beside a card, then the dashboard.
  - `people`: People ranked by your ICP, from a LinkedIn post's faceless reactions to a connection
    accepted, 27 seconds: a ranked table, the reason under a score, ticks and a record's panel.

```
TIN_FILM_FONTS=https://fonts.tin.computer/v1/private-fonts.css node web/film/tin-film.js render web/film/examples/onboarding/film.json
```

## tin-film: a film as a JSON file

`web/film/tin-film.js` is the kit as one command, for a staged film or a live site filmed from a
JSON file. It is what the `creative.feature_film` workflow's agent runs in Tin's studio sandbox
(`tin-film`, see [the creative studio](creative-studio.md#feature-films-creativefeature_film)),
and works the same from the repository:

```
node web/film/tin-film.js directions --kind idea                # five ways to show it, dealt at random
node web/film/tin-film.js sketch .films/sketches                # one still of each sketch page, side by side
node web/film/tin-film.js new .films/herding --from people     # the closest worked example, to rebuild
node web/film/tin-film.js inspect https://sheepdogs.io/ --dir .films/look   # its look as tokens, outline, screenshots
node web/film/tin-film.js record .films/herding/film.json     # once, saving what it loads from outside
node web/film/tin-film.js rehearse .films/herding/film.json   # from the recording, low resolution, checked
node web/film/tin-film.js render .films/herding/film.json     # the final take, from the recording
node web/film/tin-film.js review .films/herding/film.json     # a second model's look (in Tin's sandbox)
node web/film/tin-film.js deliver .films/herding/film.json films/herding.md
```

- **Directions before a storyboard.** A film is a product highlight (staged in the product's
  look), a motion graphic (a general idea) or both. `directions` deals cards at random from the
  kind's deck (`web/film/directions.js`: ways to picture an idea, each with a look dealt as far
  from the others as the deck allows, or ways to tell a product's story in its own look), so the
  first idea is one of several. Each direction is sketched as a stage page showing its poster
  frame (`stage.poster`), and `sketch` films each for a moment into one numbered sheet to choose
  from; the chosen page grows into the film's stage.
- **The file.** `film.json` holds `title`, `story` (`before` and `after`), `stage` or
  `site.url`, `screens`, `now` (the page's clock), `format`, `theme`, `ready`, `beats` and
  `notes`; the workflow's skill documents every field and beat
  (`codex_procedures/creative.feature_film/skills/feature-film/film-format.md` and
  `stage-kit.md`). A missing field or an unknown beat is refused before anything is filmed.
- **Pacing.** Every take warns when the first caption comes after 1.5 s, a caption shows for
  less time than its words need (17 characters a second) or more than 6.5 s, or the picture
  stands frozen for 4 s.
- **`prepare`.** Beats played before the film starts, unfilmed, for what a visitor clears first:
  a cookie banner, a popup. The hand that did it isn't in the film.
- **Beside the file.** Its recording is `world/` and its takes `takes/<record|rehearse|render>/`,
  never in the project. `inspect` opens the page through the same recording rules, so looking is
  already recording.
- **A live site's own errors** are warnings, not errors: the take goes on. Tin's dashboard still
  fails a take on any console error.
- **`review`** sends the newest take's contact sheet, marked stills, spec and check summary to
  Tin's review route with the run's grant, and prints a verdict and findings with fixes (see
  [the creative studio](creative-studio.md#feature-films-creativefeature_film)). It needs the
  run-bound grant, so it runs only in Tin's studio sandbox.
- **`deliver`** copies the final take's film (re-encoded smaller when it is over 16 MB), poster,
  WebVTT captions and `film.json`, and a staged film's stage (`stage.html`) and storyboard
  (`storyboard.json`), into `<note>.assets/`, writes the note (the film in a `tin-video` block,
  the story, every caption with its time, what the checks still found, and the film's facts),
  and checks both with `tin-studio check`, the contract Tin applies when it keeps them
  (`feature-film.v1`). **`restore <note>.md <dir>`** puts a delivered staged film back to change.
- **`deliver --directions <dir> <note>.md`** writes the note the founder picks from, before any
  film: each sketch's still and page (`N-name.jpg`, `N-name.html`) under a `## N. Name` heading
  with the sentence in its `about.txt`, a `tin-directions` block naming the recommended sketch
  and why (from `review --directions`), and `notes.json` with the maker's notes.

## Desk films

A desk film puts several windows on one desktop: a browser that shows a website or the
dashboard, and a terminal with a shell and Claude Code. `web/film/desk.js` films them in one
take, scene by scene, with the same camera, cursor and captions; `web/film/films/accelerator-demo.js`
is the example.

```
npm run film:desk -- accelerator-demo                              # one take at full pace
npm run film:desk -- accelerator-demo --pace brisk
npm run film:desk -- accelerator-demo --pace site=brisk,people=full --until files
npm run film:desk -- accelerator-demo --rehearse on                # quick, every check on
npm run film:desk -- accelerator-demo --from decisions --until decisions
```

- **Rehearsing.** `--rehearse on` (also for `npm run film`) shoots the same layout at one device px
  per CSS px instead of two, a quarter of the pixels, encodes fast, skips the GIF and the motion
  strips, and turns every check on. The brisk accelerator demo rehearses in about 2 minutes 15
  seconds; film 1's two formats in about a minute. Take the final film without it.
- **One scene.** `--from <scene>` plays the scenes before it dry: every step, wait and draw happens
  and the frame count moves on, but nothing is shot or encoded, a few ms a frame. The scenes from
  it on are filmed exactly as in a full take (the Decisions scene matched a full take on all 531
  frames), and the video, marks and captions start there. With `--until` it films a single scene.
- **Where the time goes.** Each scene in the output and `report.json` gives its render cost in ms a
  frame and its three slowest stages: the screenshot (about 50 ms of a final frame, 20 of a
  rehearsal), the probe with checks on, and each window's step. Stepping a window nobody sees costs
  about a millisecond.

- **Scenes and paces.** Each scene has a full and a brisk pace. Brisk cuts holds, types faster
  and leaves out the beats and captions marked optional. A caption stays up for its reading
  time, 0.8 s plus a second for every 15 characters, and each scene ends its own caption.
  Every scene's frames also land in their own segment, so the sheet shows each scene's length.
- **Windows.** Each window is an iframe with its own document and clock. The dashboard is laid
  out at 1440 px and shown at 1.25x, so its text reads at 1080p. A window comes forward opaque,
  and the one in front leaves by fading.
- **The terminal** recreates Claude Code from its real output: the prompt and its box, typing
  and pasting, printed lines, and a time-lapse while it works. It scrolls by whole lines.
- **Websites.** A live site, such as tin.computer, is fetched once and cached in `.films/cache/`.
- **Recorded worlds.** `web/film/replay.js` serves the dashboard from a recorded world: every
  answer the dashboard asked for while a demo project was open, scrubbed of member IDs, the
  workspace's name and its billing. A film stages its own changes on top
  (`web/film/worlds/aupilot-stage.js` and `aupilot-film.js`). A world recorded from a live
  project stays out of git, in `.films/worlds/<name>.json`; the accelerator demo needs
  `.films/worlds/aupilot.json`.

## Checks

`report.json` and the command's output list what the film got wrong. The camera is held to
Stageshow's rules: more than one push-in, a push-in past 2x or faster than 1.1 s, more than three
camera moves, or the cursor moving while the view moves are warnings. Errors exit with status 1:
the page threw or logged an error, the cursor reached a target outside the frame, or nothing was
marked `poster`. Warnings are for review: a click that moved what was clicked (the page shifted
under the cursor), a hand that steps aside and comes most of the way back (a detour of more than
80 px and 40% from a plain move, not a press), a camera that zig-zags (out and back within two
seconds, by a tenth of its frame each way), a camera that creeps (a third of a second or more of
frames moving it under 1.2 output px each), a crop that cuts a word or control in a marked frame, text under about 22 px
at 1080 in a marked frame (said once per film, smallest first, leaving out text a panel covers
or a window clips), a target the camera couldn't fit, a GIF over
15 MB or a film over 2 minutes 20 seconds. The browser test (`web/film.browser.test.js`) films a
short take twice and checks the frames match, and checks each of these rules.

### Every frame, measured

`--check on` (both `npm run film` and `npm run film:desk`) measures every element on every frame
and checks the take with mographc's film model and rules (`web/film/vendor/mographc`, MIT,
vendored without Remotion; see its README). `web/film/probe.js` reads each frame twice:

- **Each window's own page**, in its own px, where only the product moves things: an element that
  jumps more than 40 px in one frame, shows for a frame or two, or a panel, card, toast or line of
  text that pops in or out, is the product's motion, whatever the camera did. Small boxes changing
  their background in one frame (a hovered row, a ticked box) are the product changing state, and
  containers as wide as the page resize with each route; neither is reported. A desk's terminal is
  the film's own drawing and isn't checked as a product.
- **The picture**, as the viewer sees it: words cut by the frame's edge (text a page or its window
  cuts is the page's own), two windows seen through each other while one fades
  (`staging/double-exposure`), and the key frames: one taken while something is still changing
  (`film/mark-settled`, which ignores the film's own highlight, spinners and carets), or with the
  cursor's tip on words (`film/mark-cursor-on-text`).

Each finding says when, where (`picture`, or the window's id), what was measured and what the rule
requires; repeats at one moment fold into one line. Findings a film means to have are accepted with
a reason: while the camera follows the hand it crops through the screen, so words it cuts then are
accepted. Findings don't fail the take yet: they are listed in the output and in `report.json`
(`checks`, `summary`). Rules made for motion graphics stay off for filmed product: pace and dead
air (a film holds still while the product is read), competing text (a product screen is full of
it), the cursor's stretch and entrance (the hand is the system's, and hides with the window it
works in). `web/film-checks.test.js` covers each rule on synthetic frames.

Checks find what can be measured; motion still needs eyes. Before a film goes out, read the
motion strips, then give a fresh reviewer (a person, or an agent with no part in the film)
Stageshow's `references/review.md` and `verify.md`, the marks, the sheet and the strips, and fix
what they rank first. The shift and camera checks exist because a first review of stills missed a
fast 2.6x zoom that only the motion showed.

## Credit

The clock, the named easing curves, the arrow and the pointing hand, and the cursor's travel and press timing are adapted from
[Stageshow](https://github.com/benjitaylor/skills/tree/main/stageshow) (MIT, Copyright (c) 2026
Benji Taylor); its license is in `third_party/licenses/stageshow-LICENSE.txt`.
