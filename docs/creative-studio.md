# Creative studio: `creative.character` and `creative.product_demo`

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
   For voice-only retries, edit `vo` in the capture directory's `script.json`; unchanged clips
   are reused. Changes to actions or step order require editing the source script and recapturing.
4. `tin-studio frame` previews three timestamps; `tin-studio render` writes the MP4 (30 fps,
   CRF 20, loudnorm -16 LUFS, whoosh on scrolls, tap on clicks; the character's mouth follows the
   voice envelope through three shapes with hysteresis and blinks every 3.2 s);
   `tin-studio check` validates it; Codex copies it to `demos/<slug>.mp4`.
5. The runner pushes the artifact to the ephemeral branch; the switchboard validates it again,
   publishes it canonically, and asks for review.

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
- Story: the skill's word budgets (hook seven words, line fourteen words, 75 spoken words) and
  the target length (20 to 35 s) are prompt text in `codex_procedures/creative.product_demo/`.
- Contract limits: `studio_contracts.py` (64 KB SVG, 16 MB MP4, 8 to 90 s, 1080x1920).
