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
| `marks/`, `sheet.png` | Every marked frame, and a contact sheet of them for review |
| `motion/` | Every camera move, click and cut as a strip of every other frame (66ms apart), for reviewing the motion itself |
| `captions.srt` | The captions with their times, for platforms that take a caption file |
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
and `play(f)`. `people-requests.js` is the example.

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

Checks find what can be measured; motion still needs eyes. Before a film goes out, read the
motion strips, then give a fresh reviewer (a person, or an agent with no part in the film)
Stageshow's `references/review.md` and `verify.md`, the marks, the sheet and the strips, and fix
what they rank first. The shift and camera checks exist because a first review of stills missed a
fast 2.6x zoom that only the motion showed.

## Credit

The clock, the named easing curves, the arrow and the pointing hand, and the cursor's travel and press timing are adapted from
[Stageshow](https://github.com/benjitaylor/skills/tree/main/stageshow) (MIT, Copyright (c) 2026
Benji Taylor); its license is in `third_party/licenses/stageshow-LICENSE.txt`.
