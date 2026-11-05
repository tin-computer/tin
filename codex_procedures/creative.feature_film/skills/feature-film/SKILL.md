---
name: feature-film
description: Film one feature of a live product site as a captioned, checked video beside a note in Files.
---

# Feature film procedure

Everything happens through `tin-film` (run `tin-film help`). Work in a folder of your own inside
the Studio work directory the runtime names (call it WORK; API runs use
`/home/tin-work/studio`), for example `WORK/film`. The film is a JSON file there,
`WORK/film/film.json`; its recording (`world/`) and takes (`takes/`) stay beside it. Only
`tin-film deliver` writes into the project checkout.

The film plays the real site in a browser window on a virtual clock, one frame at a time, with a
drawn cursor, a camera and a caption band under the picture. Every page is recorded on its first
take and replayed from the recording after that, so every take shows the same frames and the
live site is visited once. Every rehearsal measures each frame and reports what moves badly,
what is cut off and what can't be read.

Every image you open stays in your context for the rest of the run, and a request has a fixed
size limit: a run that keeps opening images ends without its film. Open each image once, open
the contact sheet before any single still, and never reopen an earlier take's.

A take runs one to five minutes. Each check on a running command resends the whole run to the
model and counts toward its limits (256 requests, 8 million tokens), the same as a step of real
work: wait with `yield_time_ms` of 30000 or more on every check, never a few seconds. Plan
changes between takes so each take answers several questions at once.

## 1. Choose the feature and its story

1. Read `wiki/INDEX.md`: `### Feature map` under `## Product`, positioning, and what the product
   is for. Read `brand/BRAND.md` if it exists. Read the run's `feature`, `angle` and `notes`.
2. The feature is the run's `feature` when given. Otherwise pick the Feature map's most visible
   `live` feature that a public page shows. Say which line you used in the final message.
3. Write the story as one change, before and after, each one sentence in the founder's words:
   what it was like without the feature, and what it is like with it. Use `angle` when given.
   It goes in `film.json` as `story` and opens the note.

## 2. Inspect the pages

1. Run `tin-film inspect <product_url> --dir WORK/film --format <format>`. It prints the page's
   headings and controls, each with a selector that matches it alone and where it sits (`at`
   is px from the top of the page), and writes `screen.jpg` (the first screen at the film's
   width), `page.jpg` (the page, down to 6,000 px, at half width) and a starter `film.json`.
2. Open `screen.jpg` and `page.jpg` with your image viewer. Note where the feature is, what is
   on the first screen and what is in the way (a cookie banner, a popup).
3. Inspect another page the story needs the same way, with its own `--dir` (for example
   `WORK/pricing`), to read its selectors; the film itself stays in `WORK/film`.

## 3. Write the film

Edit `WORK/film/film.json` as `film-format.md` describes. The shape that works:

- Open on the feature within the first second and a half; the first caption says the change.
- Three to six captions, one idea each. A caption is at most twelve words and stays at least two
  seconds plus a third of a second per word, and at most six seconds: change it or move on.
- After the opening shot, frame part of the page, not all of it: the site's own text must read
  on a phone, so the camera closes in on what the caption names (`camera` on that section or
  card, or `text: true` on a heading).
- One thing moves at a time: the camera, then the hand, then the page. Hold still before a mark.
- `mark` `poster` on the frame that tells the story at a glance, the feature in view and nothing
  moving. Mark one or two more key moments.
- Close on the result, then `end`.
- 15 to 40 seconds in all.

Every caption claim is visible on screen while it shows, or comes from a Feature map line marked
`observed`. Write captions as the founder would say them to a customer: present tense, plain
words, the thing itself. Follow the project's writing style skill when it is there.

## 4. Record, rehearse, fix

1. `tin-film record WORK/film/film.json` plays the film once against the live site and saves
   every answer the pages get. Requests that would send, publish or pay are stopped and named;
   trackers get an empty answer. Run it again whenever the film visits something new.
2. `tin-film rehearse WORK/film/film.json` plays it from the recording at low resolution with
   every check on. It prints errors, warnings and check findings, and writes the takes' stills.
3. Open the take's `sheet.jpg` with your image viewer: every marked frame, small. Check that
   each page loaded, the caption reads, the camera frames what the caption names, nothing
   covers the feature, and the hand points at what it says. Open a single still in `marks/`
   only for a detail the sheet is too small to show.
4. Fix the film from what you saw and what was printed, then rehearse again:
   - "has no answer for N requests": record again.
   - "Nothing on screen matches": the selector is wrong or the element isn't there yet; take a
     selector from inspect, or `ready` on it first.
   - A crop that cuts text: frame a larger target, or add `pad`.
   - Text under 22px: frame closer with `camera` on a smaller target.
   - A finding about a jump, a flicker or something popping in: hold longer before the mark, or
     frame around it. If it is the site's own motion, leave it and say so in `notes`.
   - The font didn't load: add a `hold` of a second after the page arrives.
5. Stop when a rehearsal has no errors and the stills are right. Findings the site causes
   itself may stay; name each one in `notes` with why, one sentence each. `notes` is for the
   founder reading the film: nothing about the run, its sources or its ID. Rehearse at most eight times; then
   render the best version and say what is left.

## 5. Render and deliver

1. `tin-film render WORK/film/film.json` makes the final take at full resolution. Look at its
   `sheet.png` once.
2. `tin-film deliver WORK/film/film.json /home/user/project/films/<slug>.md` copies the film,
   its poster, its captions and `film.json` into `films/<slug>.assets/`, writes the note and
   checks both as Tin will. It must print `OK`. Never edit the note by hand: change `film.json`
   and deliver again.
3. The final message: the feature line you filmed, the story, each caption with its time, the
   length, and every finding left with its reason.

## Rules

Stay on public pages. Never sign up, log in, submit a form, or press buy, send, delete, invite,
cancel or contact. Typing into a search box is fine; submitting anything that sends is not. Never
change the page to fake a state. Create no file in the project checkout other than the note and
its assets folder, which `tin-film deliver` writes.
