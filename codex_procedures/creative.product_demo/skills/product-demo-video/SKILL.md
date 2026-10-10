---
name: product-demo-video
description: Capture a live product at phone size and render a narrated 9:16 demo video.
---

# Product demo video procedure

Everything happens through `tin-studio` (run `tin-studio help`). Work in
the Studio work directory provided by the runtime (call it WORK; API runs use
`/home/tin-work/studio`, historical OAuth runs use `/home/user/.tin-lite/studio`);
only the finished MP4 goes into the project
checkout. There is no real-time screen recording: you capture settled keyframes and the renderer
synthesizes the scroll, tap, zoom, and caption motion, which is why the result is smooth.

## 1. Plan the story

1. Read `wiki/INDEX.md` (`## Product`, feature map, positioning). Read `angle` and `notes`.
2. Run `tin-studio inspect <product_url> WORK/inspect`. Read the JSON: `headings` with their
   `scrollY`, `actions` with ready-made `selector`s (prefer ones with `inHeader: true` for
   navigation), `text`, `docHeight`. Open `WORK/inspect/inspect.png` and
   `WORK/inspect/inspect-full.png` with your image viewer and note what each screen shows.
   Inspect one or two more pages if the story needs them (docs, pricing).
3. Decide the format `script-format.md` describes: one hook of at most seven words that names
   the pain (`angle` if given), then five to seven steps. Each step is one visible moment with
   one voice line of at most fourteen words and one caption of at most five words. Mark exactly
   one `*word*` per caption and in the hook. The last step lands on the call to action the page
   actually offers (trial, pricing, install) with a `[warm]` line. Total speech at most 75 words.
4. Claims come from the page text or project memory; numbers must be visible on screen in the
   step that says them.
5. Write every line as a creator talking to the viewer: present tense, second person, the
   thing itself. Never narrate the page ("the page shows", "this section lists", "the site's
   example"); say what it means for them ("your agent gets its own number"). Write amounts and
   counts as words, and keep brand names inside a full sentence.

## 2. Write and capture

1. Write `WORK/script.json` in the format from `script-format.md`.
2. Run `tin-studio capture WORK/script.json WORK/out`. Open every `WORK/out/frames/kNNN.png`
   with your image viewer and check: the page loaded (no blank, no error, no interstitial); no
   cookie banner or popup covers the content (add an early `click` step on its dismiss button
   if one appears, or choose a page without it); each scroll lands where the caption expects;
   each click hit the intended element (the `click` keyframe shows the new page). A keyframe
   that is wrong is a script fix: better `selector` from inspect, `scrollTo`, a bigger
   `settleMs`, or a different page. Recapture until every keyframe is right.

## 3. Voice

1. Run `tin-studio voice WORK/out --voice <voice> --language "<language>"`. Add `[excited]`
   on the hook line and `[warm]` on the last; otherwise plain sentences. Each run prints the
   clip length per step; if a clip is longer than five seconds, shorten that line.
2. Voice is capped per run (24 lines, 3000 characters). Re-running keeps unchanged lines, so
   edit the `vo` line in `WORK/out/script.json` (the captured copy that `voice` reads), then
   run `voice` again. Voice-only edits need no recapture. Keep the steps and actions unchanged.
   To add, remove, reorder, or change actions, edit `WORK/script.json` and recapture first.
3. A line the provider refuses is reported as `REFUSED` with the reason and the other lines
   are kept. A `content_policy_violation` is the provider's opaque text checker misfiring:
   first run `voice` again without any `--style` of your own (the default style is known to
   pass), then rewrite that one line with plainer wording and run `voice` again. Never resend
   an unchanged refused line, and never spend more than two rewrites on one line; drop it and
   let the caption carry the step.

## 4. Preview, render, check

1. Character: when `character` is set, the file is
   `/home/user/project/characters/<character>.svg`; run `tin-studio check` on it first. If it
   is missing or rejected, render without it and say so in the message.
2. Preview three frames before the full render (hook, a mid scroll, the final step):
   `tin-studio frame WORK/out WORK/p1.png --at 1.5 --bg <backdrop> [--character <svg>]`,
   and again at about half the total length and at the end. Look at them: captions readable
   and centred, the character's face visible and not covering the caption, the phone card
   showing the right screen, backdrop not clashing.
3. Render: `tin-studio render WORK/out WORK/<slug>.mp4 --bg <backdrop> [--character <svg>]`.
   It prints the duration. Then `tin-studio check WORK/<slug>.mp4` must print `OK` and
   `tin-studio probe` should show 1080x1920 with an audio stream.
4. Longer than 40 seconds: shorten lines in `WORK/out/script.json`, re-run `voice`, and render
   again. To cut a step, edit `WORK/script.json` and recapture before voice and render.

## 5. Deliver

Copy the checked MP4 to the declared output path and nothing else. The final message is the
storyboard: hook, then per step the caption, the voice line, and what is on screen; the total
duration, voice, backdrop, and character used; and any claim you softened because the page did
not support it.

## Rules

Never sign up, log in, fill a form, or press buy, send, delete, invite, or contact. Do not use
any account. Do not capture pages behind a login. Do not edit the page's DOM to fake a state.
Do not create any file in the project checkout other than the declared output.
