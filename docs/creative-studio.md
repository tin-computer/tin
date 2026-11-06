# Creative studio: `creative.character`, `creative.product_demo` and `creative.feature_film`

Written 2026-09-08. Two `codex.procedure` workflows that turn a product's public pages into a
brand character (an animatable SVG) and a narrated 9:16 demo video, the way a human editor would
make a TikTok or Reels product clip: hook, screen recording, voiceover, word-synced captions, a
mascot reacting in the corner. The design record is the "Creative studio outcome" in
`AGENTS.md`; this file is the operator walkthrough.

## Shape

| Piece | Where | What it does |
|---|---|---|
| Studio sandbox image | `sandbox/template.py` (`tin-lite-codex-studio`) | browser image + `sandbox/studio/` toolkit, ffmpeg, resvg, Pillow, Montserrat; 4 vCPU |
| Toolkit | `sandbox/studio/tin_studio.py` and siblings | `inspect`, `capture`, `voice`, `render`, `frame`, `rasterize`, `check`, `probe` |
| Contracts | `sandbox/studio/studio_contracts.py` = `src/tin_lite/studio_contracts.py` | character SVG validator, MP4 validator, state flipping; a test keeps the copies identical |
| Voice route | `src/tin_lite/run_tools.py`, `src/tin_lite/studio.py` | `POST /internal/run-tools/studio/voice` with the run grant; fal Gemini TTS + Whisper words; receipts + quotas |
| Grants | migration `027_studio_run_tools.sql` | `run_tool_grants.provider_key = 'tin.studio'` with `connection_id NULL` |
| Character workflow | `src/tin_lite/character_design.py`, `character_design_activities.py`, `example_character.svg` | page fetch and extraction, prompts, schema, validate/repair/refine, Temporal activities; route `creative.character.v1` = gpt-6-sol medium |
| Demo procedure | `codex_procedures/creative.product_demo/` | prompt + skill + `script-format.md` |
| Catalog | `src/tin_lite/catalog.py` | system `creative-studio`, ids `…021` and `…022`, review-eligible, on demand only |
| UI | `src/tin_lite/static/app.js` | Files renders `.svg` and plays `.mp4` from an authenticated blob; review approves from that view |

## How a demo run works

1. The switchboard creates a studio sandbox, mints a `tin.studio` grant, and starts Codex with
   the `product-demo-video` skill.
2. Codex runs `tin-studio inspect <url> WORK/inspect` (headings with scroll offsets, clickable
   actions with selectors, brand colors, a phone screenshot), writes `WORK/script.json`
   (hook, five to seven steps, each with a caption and a voice line), and runs
   `tin-studio capture`. It looks at every keyframe PNG with its image tool and fixes the script
   until each is right.
3. `tin-studio voice` posts each line to the switchboard route; the switchboard calls fal, stores
   one receipt per line, and returns the mp3 plus the words heard. The sandbox aligns those words
   onto the script text (difflib) so the captions show the written words at the spoken times.
   For voice-only retries, edit `vo` in the capture directory's `script.json` and copy that
   edit to the source script; unchanged clips are reused. Keep both copies in sync so later
   recaptures preserve the corrected line. Changes to actions or step order need a recapture.
4. `tin-studio frame` previews three timestamps; `tin-studio render` writes the MP4 (30 fps,
   CRF 20, loudnorm -16 LUFS, whoosh on scrolls, tap on clicks; the character's mouth follows the
   voice envelope through three shapes with hysteresis and blinks every 3.2 s);
   `tin-studio check` validates it; Codex copies it to `demos/<slug>.mp4`.
5. The runner pushes the artifact to the ephemeral branch; the switchboard validates it again,
   publishes it canonically, and asks for review.

## Feature films: `creative.feature_film`

Added 2026-11-05. A `codex.procedure` on the same studio profile that makes a short captioned
film of one feature for a LinkedIn or X post: a staged scene in the product's own look, where one
thing happens per beat and the result stays on screen, played on a virtual clock one frame at a
time with a drawn cursor and a caption band. It is the film kit (`web/film`,
[feature films](feature-films.md#staged-films)) and its stage kit as the `tin-film` command.

| Piece | Where | What it does |
|---|---|---|
| Toolkit | `web/film/tin-film.js`, `inspect.js`, the kit and `web/film/stage/`, in the image at `/opt/tin-lite/diagram/web/film/` | `new`, `inspect`, `record`, `rehearse`, `render`, `deliver`; runs on the diagram layer's pinned Playwright Chromium (`sandbox/tin_film.sh`) |
| Worked examples | `web/film/examples/onboarding/`, `people/` | Tin's own onboarding and People films, each a storyboard, a `film.json` and a stage: the bar `tin-film new --from` copies |
| Contract | `studio_contracts.parse_feature_film`, `validate_feature_film_video` (both copies) | the note plays exactly one film from its own folder; the film is a fast-start MP4 at 1080x1350, 1080x1080 or 1920x1080, 8 to 90 s |
| Procedure | `codex_procedures/creative.feature_film/` | prompt, skill, `stage-kit.md` (the parts, the runtime, framing) and `film-format.md` (the JSON film and its beats) |
| Catalog | `src/tin_lite/catalog.py` | id `…049`, `FEATURE_FILM_REVIEW_POLICY`, on demand, inputs `slug`, `feature`, `post`, `angle`, `product_url` (optional, for the look), `format`, `notes` |

How a run goes:

1. The agent works out the feature (the run's `feature` and `post`, or the Feature map) and what
   really happens when someone uses it, in the product's real wording; with a page, `tin-film
   inspect` reads its look as the stage kit's tokens, its fonts and a screenshot.
2. `tin-film new --from` copies the worked example closest to the feature; the agent rehearses
   it once and looks at its contact sheet, one still per shot: the bar.
3. It rewrites the example's `storyboard.md` for this feature (the story as a before and an
   after, three to six shots and a payoff, each with its mark, caption, hero, satellite, real
   words and what moves), then rebuilds the stage for this product (its tokens, its chrome, the
   kit's parts, sample content only) and writes the beats, a caption and a mark per shot.
4. `tin-film rehearse` plays it at low resolution with every frame measured and checked
   (mographc's rules, the kit's own, the pacing checks and the storyboard's); its contact sheet
   is the storyboard drawn, and the agent fixes the film until it is clean.
5. `tin-film render` makes the final take, and `tin-film deliver` writes `films/{slug}.md` with
   `film.mp4`, `poster.png`, `captions.vtt` and `film.json` in `films/{slug}.assets/`, then runs
   `tin-studio check` on both.
6. Tin keeps the note (`feature-film.v1`) and its files (`page_assets` kinds mp4, png, vtt and
   json; every file leak-scanned), and the review shows the note with the film playing.
   Approval binds the note and every file's checksum. Tin posts nothing.

Version 1.0.0 stages what Tin holds about the product in its public look and signs in nowhere;
captions only, one format a run. A second model's look at the stills and video on LinkedIn drafts
come next.

## How a character run works (no sandbox)

1. `character_design` activity, step "prepare": read `wiki/INDEX.md` and fetch the product page
   on the switchboard (HTTPS only, resolved address must be public, 1.5 MB HTML, three
   stylesheets). Extract title, description, headings, button and link labels, 6,000 chars of
   text, JSON-LD and Open Graph facts, and a brand palette weighted by CSS context (button, CTA,
   `:root` and theme-color colors outrank syntax-highlighting themes). Saved as the
   `character_context` receipt.
2. Step "design": one strict-schema request to gpt-6-sol (medium reasoning) with the
   character-design rules as the system prompt and a JSON payload (brief, notes, memory, page
   facts, the example character). The model returns audience, product noun, concept, palette,
   SVG. The SVG is checked by `studio_contracts.validate_character_svg` plus geometry checks
   (face box, shared mouth anchor, hidden states); complaints go back for at most two repairs;
   then a refinement call reviews a seven-point checklist. Saved as the `character_model`
   receipt with per-call timings and usage.
3. Step "publish": the SVG is committed to `characters/<slug>.svg` (`character_commit`
   receipt), the run enters review (`character_review`), approval is recorded, and
   `character_project` marks it succeeded. The `character_designed` Activity event carries the
   timings.

Measured on 2026-09-09/10 against clawmessenger.com, tin.computer, imagetextedit.com, and
foragearound.com: draft 43-63 s, refinement 41-63 s, 87 s end to end on the switchboard for
clawmessenger.com. The retired agent version took 150-184 s from sandbox creation to artifact
plus sandbox start and queueing. Quality was comparable on the four sites (on-brand palettes,
product-specific props, all states distinct); the model version misses nothing the agent's
browser saw except live-rendered pages, which the JSON-LD/Open Graph fallback covers.

## Operating

- Build the API image: `uv run python sandbox/template.py --only tin-lite-codex-studio-api`.
  The historical OAuth image remains `tin-lite-codex-studio`. Rebuild after changes under
  `sandbox/`.
- Verify the API image: `uv run python scripts/verify_studio_sandbox.py --api --url https://<public page>/`.
- Configure: `FAL_KEY` in the switchboard `.env`. Billed Studio execution requires an
  admin-scoped key for per-request billing records. According to fal's current
  [scope documentation](https://fal.ai/docs/documentation/setting-up/authentication), ADMIN
  also includes model inference; this supersedes the earlier management-only diagnosis.
  Verify both access paths with the configured key before enabling the hosted rollout;
  see [Studio API acceptance](studio-api-and-hosted-credits.md). Optional quotas
  `TIN_LITE_STUDIO_MAX_VOICE_LINES` (24) and `TIN_LITE_STUDIO_MAX_VOICE_CHARACTERS` (3000).
  If fal reports `User is locked` (unpaid balance), voice fails closed with a 422 and the run
  fails; top up at fal.ai billing.
- Apply migration 027 before deploying the runtime (`uv run tin-lite migrate`).
- Inspect a run: `workflow_runs`, `effect_receipts` with `operation = 'studio_voice'` and
  `execution_key LIKE '<run_id>:studio-voice:%'`, `run_tool_grants` with
  `provider_key = 'tin.studio'`, and `uv run tin-lite rollouts <run_id> --trace` for what Codex
  did with the keyframes.

## Tuning dials

- Look: `sandbox/studio/render.py` constants (`CARD_*`, `MASCOT_PX`, caption size 80 px,
  hook 92 px, `BACKDROPS`), the mouth thresholds in the mascot block, `--fps`.
- Voice: `DEFAULT_VOICE_STYLE` and `STUDIO_VOICES` in `src/tin_lite/studio.py`; the fal
  model ids `TTS_URL` and `STT_URL` there.
  Omit `--style` to keep the default; use `--style ""` to send no style instructions.
  This allows an unstyled retry when the provider speaks the style text aloud.
- Story: the skill's word budgets (hook seven words, line fourteen words, 75 spoken words) and
  the target length (20 to 35 s) are prompt text in `codex_procedures/creative.product_demo/`.
- Contract limits: `studio_contracts.py` (64 KB SVG, 16 MB MP4, 8 to 90 s, 1080x1920).
