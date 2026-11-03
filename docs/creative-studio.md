# Creative studio: characters, demos and launch films

The studio makes a brand character (an animatable SVG), a narrated 9:16 demo video, or a
launch film with a chosen story, format, length and sound. The demo captures public product
pages for a TikTok or Reels clip: hook, screen recording, voiceover, word-synced captions and
an optional mascot reacting in the corner. This guide covers the contracts and operator checks.

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
| Launch workflow | `src/tin_lite/launch_video.py`, `launch_video_activities.py`, `launch_directions.md` | direct Anthropic story/code generation, bounded repairs, offline render and Files review |
| Catalog | `src/tin_lite/catalog.py` | system `creative-studio`, review-eligible, on demand only; launch film id `…048` |
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

## How a launch film run works

`creative.launch_video` is a native workflow, exposed as `start_launch_video`. Story and
film source calls go directly to Anthropic (`claude-opus-5-5`) using the server's
`ANTHROPIC_API_KEY`. They do not use Codex, OpenRouter or a Claude subscription.
The reusable key never enters the renderer. The pinned supplier card caps a run at $6;
actual observed usage is charged, not the ceiling. Narration uses the existing Studio voice
service and its pinned prices within that ceiling.
The calling coding agent collects four answers **before** starting the background run:

1. Which feature or customer moment should the film show?
2. What story and filming approach fit? Offer a few product-specific ideas when useful;
   the user can choose, describe another direction, or delegate the choice.
3. Where will it be used, in which format, and for how many seconds? Destination, format
   and duration are explicit inputs. Do not infer a ratio from a platform or assume 20 seconds.
4. Should it use music, voice with music, or silence? When narrated, ask whether to show
   timed speech captions and preserve that answer.

Reuse earlier answers. Record user choices separately from agent choices. Read the product,
Feature map, `BRAND.md` and `DESIGN.md` from the saved project snapshot rather than asking
the founder to describe them again. Context reads are bounded to 8 KB, 6 KB and 6 KB.
Missing files do not justify invented claims. This first native version does not browse the
live product or search stock/music providers. The caller must select any approved project
images/fonts and music before starting.

The required inputs are `slug`, `feature_moment`, `platform`, `format`, `duration_seconds`
and `sound_mode`. `story_preference` and `filming_preference` preserve chosen directions;
empty preferences delegate those decisions. `notes` carries prior answers, exclusions,
approved assets and any requested ending. `captions` is required for `voice_and_music`.
For either music mode, `music_path` selects an existing project audio file and
`music_license` records its cleared source and usage rights. This is a caller assertion,
not an automatic license verification. Only the MP4 is delivered, so select a track that
needs no separate public attribution. `asset_paths` selects at most 12 PNG/JPEG/WebP or
WOFF/WOFF2 files (20 MB total); SVG logos and video clips are not supported in this version.

| Input | Accepted values |
|---|---|
| `format` | `landscape` (1920x1080), `portrait` (1080x1920), `square` (1080x1080) |
| `duration_seconds` | An integer from 8 to 90; no default |
| `sound_mode` | `music`, `voice_and_music`, `silent`; no default |
| `captions` | `true` or `false`, explicitly selected when narrated |

The model direction guide offers eight story moves, six filming approaches and three cinematic examples.
They are prompts for making a product-specific story, not a fixed template or a required
customer menu. The opening must show a recognizable problem or product action; the result
must be visible. Use few words, supported claims and an intentional ending. Do not add a
default call to action, decorative eyebrows or production labels to the picture.

Trusted code checks selected assets and the Studio image before buying a draft, then measures
the selected music from time zero. Its RMS/onset cues guide scene timing; they are not a verified
BPM map, a listening test or evidence that a track fits the emotion. A missing track fails the
run; it never silently becomes a silent film.

Anthropic returns a bounded JSON object with one story sentence, local HTML and optional short
narration. Code owns the sequence, not model tool calls. Each generated draft is saved in a
run receipt. Failed source/media checks can buy at most two repairs; unresolved model calls
are never automatically repurchased. Missing services stop the run instead of buying repairs.

Each render uses a fresh Studio API sandbox with all network egress blocked. It receives only
the HTML, selected public assets and audio, never a project checkout or provider credentials.
The page can read only its film folder. The source review checks layout, production labels and
seek determinism. Narration uses one line, at most 400 characters, and the server supplies
optional captions from measured speech timing. Silent mode adds a compatible silent audio track.

The runtime commands cover the finished output:

```sh
tin-launch stills --page film/index.html --at 0,5,9 --outdir stills --w 1920 --h 1080
tin-launch review --page film/index.html --outdir review --w 1920 --h 1080 --fps 30
tin-launch render --page film/index.html --out silent.mp4 --w 1920 --h 1080 --fps 30
tin-launch analyse --music approved-track.mp3 --start 0 --duration 10 --outdir music-cut
tin-launch mix --video silent.mp4 --music music-cut/music.wav --out final.mp4
tin-launch check final.mp4 --format landscape --duration 10
```

These example times and dimensions must be replaced with the selected brief. Mixing can
also take `--effects`, `--voice`, or `--silent`. The native runner performs source review and full codec/decode checks. These do not prove
that a story is clear, its UI is faithful or the audio sounds good. The final human review
must watch and listen; no automatic vision or listening judgment is claimed.

Save the actual finished file as `launches/<slug>.mp4`, at most 16 MB. A source page, contact
sheet or checklist does not satisfy the output contract. Tin validates it with
`launch-video.v1` and presents its own launch-film review. Approval keeps the film in Files
for the founder to share; it does not post it.

This source integration and its fixture checks do not prove hosted or staging acceptance.
Rebuild the Studio API image before a hosted run, configure `ANTHROPIC_API_KEY`, then validate
the actual workflow there. A local renderer test is not a paid Anthropic or hosted workflow run.

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
- Contract limits: `studio_contracts.py` (64 KB SVG; 16 MB MP4, 8 to 90 s; demos stay
  1080x1920 while launch films support landscape, portrait and square Full HD).
