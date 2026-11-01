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
cards and the person panel never end up under a caption.

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
| `f.camera(target, {pad, duration, cut, wait, text, inside})` | Frames the target (a selector, a list of selectors, or a rect). `text` frames only the words inside, `inside` keeps the frame within a region (`f.inside` sets a default), `wait: false` lets the move run under the next calls. Never zooms past the device scale. |
| `f.move(target, {to})` | The cursor travels in a shallow arc; the real pointer follows, so hovers happen. |
| `f.click(target)` | Moves, rests, presses: the cursor dips, a ring spreads and the page gets a real click. |
| `f.type(text)`, `f.scroll(container, target)` | Real keystrokes; an eased scroll of a container. |
| `f.caption(text)` | One caption at a time; the last fades out and the next rises in. `null` clears it. |
| `f.hold(seconds)`, `f.ready(selector)` | Lets time pass; waits (filmed) until something is visible. |
| `f.open(route)`, `f.cut(change)` | Opens a view; fades to the ground, does `change` unfilmed, fades back. |
| `f.mark(name)` | Keeps the frame just shot for the sheet. `poster` is required. |
| `f.world`, `f.wide` | The served world, to move it on behind a cut; whether the format is wide. |

How a good film reads, after Stageshow's guidance:

- one feature, three to five beats and a payoff, about 15 to 25 seconds;
- open on a finished, readable screen; frame the part the story happens in;
- one short caption per beat, in sentence case; feeds play muted, so the captions carry it;
- the cursor moves only with a cause, and rests on empty surface;
- the poster is the decision moment, the frame that tells the story alone;
- everyone and everything in the demo world is made up.

## Checks

`report.json` and the command's output list what the film got wrong. Errors exit with status 1:
the page threw or logged an error, the cursor reached a target outside the frame, or nothing was
marked `poster`. Warnings are for review: text under about 22 px at 1080 in a marked frame, a
target the camera couldn't fit, a GIF over 15 MB or a film over 2 minutes 20 seconds. The
browser test (`web/film.browser.test.js`) films a short take twice and checks the frames match.

## Credit

The clock, the cursor's look and its travel and press timing are adapted from
[Stageshow](https://github.com/benjitaylor/skills/tree/main/stageshow) (MIT, Copyright (c) 2026
Benji Taylor); its license is in `third_party/licenses/stageshow-LICENSE.txt`.
