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

The caption band sits under the picture, outside the dashboard's screen: the window is a band's
height taller than the screen, and the body is the box fixed elements sit in, so toasts, hover
cards and the person panel never end up under a caption. A film can pick its screen per format
(`screens`); end it where the picture ends, so a toast at its foot sits clear of the edge. Where
the picture cuts through a list, the list fades into the band.

## How it works

- **The demo world.** `serveApp` from `web/app-fixture.js` serves the dashboard with synthetic
  APIs, given a film's own people, connections and numbers (`web/film/world.js` builds them).
  There is no database and no account. Every request to anywhere else is blocked and reported,
  and the server's clock is the film's day, so what it stamps reads as recent.
- **The clock.** `web/film/clock.js` replaces the page's timers, animation frames, CSS animations
  and `Date` before the dashboard loads. Time moves only when the recorder steps it, by 1/30 s.
  Every request lands in the frame that made it, so two takes are identical.
- **The director.** The script calls `f.camera`, `f.move`, `f.click` and so on; each frame the
  recorder moves the camera and cursor, steps the clock, draws the overlay (cursor, ripple,
  caption band, fade) and screenshots the camera's part of the screen. ffmpeg scales each frame
  to the output size. Clicks, hovers and typing are real input to the real dashboard.
- **Fonts.** Public builds film in Geist. `--fonts` (or `TIN_FILM_FONTS`) names a licensed
  stylesheet; the recorder fetches it and its files and serves them from the film's own origin,
  where `index.html` asks for the private stylesheet. Nothing licensed is committed.

## Writing a film

A film is a module in `web/film/films/` exporting `now` (the film's day), `timezone`, `world()`
and `play(f)`. `people-requests.js` is the example.

| Call | What it does |
| --- | --- |
| `f.camera(target, {pad, duration, curve, cut, text, inside})` | Frames the target (a selector, a list of selectors, or a rect). `text` frames only the words inside, `inside` keeps the frame within a region (`f.inside` sets a default). 1.1 s on Stageshow's camera curve by default (`house` for a slow, longer move); never zooms past the device scale. |
| `f.move(target, {to})` | The cursor travels in a shallow arc. The page sees the pointer only where the hand stops: a hover ends as it moves off, and nothing it passes over lights up. |
| `f.click(target)` | Moves, rests, presses: the cursor dips, a ring spreads and the page gets a real click. The ring fades early if what was pressed goes. The cursor is a pointing hand wherever the page's own cursor would be a pointer. |
| `f.type(text)`, `f.scroll(container, target)` | Real keystrokes; an eased scroll of a container. |
| `f.caption(text)` | One caption at a time; the last fades out and the next rises in. `null` clears it. |
| `f.hold(seconds)`, `f.ready(selector)` | Lets time pass; waits (filmed) until something is visible. |
| `f.open(route)`, `f.cut(change, {caption})` | Opens a view; fades to the ground into the cut and settles out of it, doing `change` unfilmed (open a view, move the world on, let time pass); `caption` arrives with the new scene. |
| `f.highlight(target, {hold, delay, pad, over})` | A soft mark on what just changed: it washes in, holds and fades. It is drawn in the page, under its toasts and inside its fades; `over` puts it above a panel. |
| `f.begin()`, `f.end()` | Fades in from the band's ground (from 85%, so the first frame isn't dark) and out to it. |
| `f.shows(selector)` | Whether the target is on screen and on top: not scrolled out of its panel or covered. |
| `f.mark(name)` | Keeps the frame just shot for the sheet. `poster` is required. |
| `f.world`, `f.wide` | The served world, to move it on behind a cut; whether the format is wide. |

How a good film reads, after Stageshow's guidance (its `references/motion.md` and `framing.md`):

- one feature, three to five beats and a payoff, about 15 to 20 seconds;
- open on a finished, readable screen, and hold one framing that works as a still: the camera
  needs a reason, at most one push-in (or pull-out) per film, slow, within 2x, with the cursor
  still while the view moves;
- the cursor moves only with a cause, aims inside its target but off its words, and rests on
  empty surface between beats;
- a cut lets time pass behind it and changes only what time changed, so nothing from the last
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
under the cursor), text under about 22 px at 1080 in a marked frame (said once per film,
smallest first, leaving out text a panel covers), a target the camera couldn't fit, a GIF over
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
