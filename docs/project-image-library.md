# Project image library

Written 2026-11-19. Each project keeps its own library of illustrations, icons, photos and
diagrams in project Files under `brand/illustrations/`. A member's coding agent searches it
through MCP before it draws anything new, and every new drawing goes through Studio's fal path,
so each one has receipts and usage like the voice and character work. Members see the library
in the dashboard at Brand > Images. There is no public link.

## Shape

| Piece | Where | What it does |
|---|---|---|
| Library service | `src/tin_lite/image_library.py` | paths, sidecars, `INDEX.json`, `STYLE.json`, search, save, discard, download links |
| Drawing run | `src/tin_lite/image_generation_activities.py`, `ImageGenerationWorkflow` in `workflows.py` | agent-only `brand.image` run: draws, checks, saves, writes the receipts |
| fal call | `StudioService.image` in `src/tin_lite/studio.py` | one effect receipt per attempt, an `external_usage_v1` receipt, cost from fal billing events |
| Rate card | `IMAGE_CARD` in `src/tin_lite/studio_billing.py` | step `image`, at most $2 per request, fal's reported cost |
| MCP tools | `src/tin_lite/mcp_server.py` | `search_images`, `get_image`, `generate_image`, `check_generation`, `discard_image`, `get_image_style`, `set_image_style` |
| HTTP | `src/tin_lite/image_library_api.py` | the Brand > Images list and discard, and `GET /api/file-downloads/{token}` |
| Links table | migration `085_file_download_links.sql` | one row per download link, hashed token, 15 minutes |
| Dashboard | `static/brand-images.js`, `static/brand-images.css`, the Brand nav item | grid by kind, search, download, discard |
| Catalog | `src/tin_lite/catalog.py` | workflow id `…048`, system `creative-studio`, `agent_only`, Tin-funded in `billing.py` |

## Files in a project

```
brand/illustrations/
  STYLE.json                  house style per kind, model per kind, references, version
  INDEX.json                  every image, newest first, for search
  reference/                  reference images the style points at
  illustration/<slug>.png     the drawing
  illustration/<slug>.json    its sidecar
  icon/<slug>.png (+ .svg)    vector icons keep an .svg next to the .png
  photo/<slug>.png
  diagram/<slug>.mmd          tin-diagram source, drawn by the browser renderer
```

A sidecar holds the subject, tags, kind, model, prompt, style version, author, created time,
the run id, any checks the drawing failed, and a status of `saved` or `retired`. The first save
also adds one line for the library to `wiki/INDEX.md`, so every run that reads the wiki finds it.

Files limits apply: PNG and JPEG up to 5 MB and 8192 px, SVG text up to 1 MB. An SVG is never
served as a page. The dashboard shows it as a data URL inside an `<img>`, and the download link
sends it as an attachment with the raw-file sandbox headers (`docs/project-file-security.md`).

## How an agent uses it

1. `search_images` with words from the subject. Reuse a match when there is one.
2. `get_image_style` for the kind. A kind with no style text cannot be drawn: the agent sets one
   with `set_image_style` after asking the founder, and may point it at reference images it
   uploaded to `brand/illustrations/reference/` with `prepare_file_upload`.
3. `generate_image` with a concrete subject ("a hand turning a brass key", not "security"), a
   kind, a size (`square`, `landscape`, `portrait`, `wide`), a ground (`transparent`, `light`,
   `dark`, `any`) and tags. It returns a `job_id` at once. A repeat with the same `request_id`
   returns the same job.
4. `check_generation` about a minute later. While the run works it says to check again in 30
   seconds. When it is done it returns the saved image, its paths and download links.
5. `get_image` gives fresh download links, each good for 15 minutes for one file at one revision,
   for the member who asked.

A diagram takes no model. The agent writes tin-diagram source and passes it as `diagram_source`;
Tin checks it with the diagram parser and saves it at once, with no run.

## How a drawing run works

1. `generate_image` starts a `brand.image` run. Run admission refuses this workflow from
   anywhere else, including the dashboard's retry button.
2. The run reads the kind's style. It uses the edit model when the style has reference images
   (up to four are sent) and the text model when it has none.
3. It draws through `StudioService.image`. Each attempt has its own effect receipt, so a retried
   activity replays the stored result and never buys the same drawing twice. An attempt left
   unresolved stops the run with "no repeat purchase".
4. It checks the drawing: the ground must be transparent when asked, and the shape must be
   within 6% of the size asked. A failed check draws once more. If the second drawing fails too,
   Tin keeps it, because it was paid for, and writes the problems into the sidecar's `checks`.
5. It saves the image, sidecar, `INDEX.json` and the wiki line in one Files commit, then marks
   the run succeeded with the image as its artifact.

The run saves the image itself. `check_generation` only reads the result, so an agent that never
checks still leaves the image in the library.

## Discard and retire

Within 48 hours of its save, `discard_image` (or Discard in the dashboard) deletes the image and
sidecar in a new commit and removes it from `INDEX.json`. Files history still has it. After 48
hours the files stay and the sidecar's status becomes `retired`; search leaves retired images
out, and the dashboard shows them only on request.

## Models

`STYLE.json` names a model per kind. With none set, the server default in `DEFAULT_MODELS`
applies. The defaults are placeholders until the bake-off picks winners:

| Kind | With references | Without |
|---|---|---|
| illustration | `google/nano-banana-2.1/edit` | `google/nano-banana-2.1` |
| icon | `recraft/v4` | `recraft/v4` |
| photo | `bytedance/seedream/v5.5/edit` | `bytedance/seedream/v5.5/text-to-image` |

`set_image_style` accepts any fal endpoint from the four families Tin knows how to call:
nano-banana, seedream, gpt-image and recraft. An empty model resets the kind to the default.

## Deploying

- Apply migration `085_file_download_links.sql`.
- Sync the catalog so the `brand.image` workflow row exists.
- The switchboard needs `FAL_KEY`, which it already has for Studio. No new variable.
- Restart the worker so it registers `image_generate` and `image_failure`.

## Tests

`tests/test_image_library.py` covers the contract, the Files layout, download links, discard and
retire, a drawing run with receipts and replay, and the MCP tools. fal is a mock transport;
no test calls a provider.
