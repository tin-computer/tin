# Brand and design capture

`brand.capture` is a public, on-demand Codex procedure. It inspects a product website,
an optional selected GitHub repository, or an attributed source packet already in project
Files. One review adopts `brand/BRAND.md` and `DESIGN.md` together. It does not edit the
website, create a PR or generate an asset kit.

This slice fills missing guidance. An existing compatible document is carried forward
byte-for-byte, including member annotations and its own section layout. If both files
already exist, starting capture fails before compute; use Files for small edits. Existing
BRAND.md needs valid `tin-brand.v1` tokens, and both files must fit the documented bounds.
Refresh and partial replacement are deferred. `content.design_md` remains available;
this change does not archive its saved configurations or remove code-backed capture.

## Sources and inputs

The normal Registry form accepts an optional HTTPS product URL, notes about protected
choices, `capture` or `develop` intent, optional repository evidence (enabled by default),
and an optional Markdown `source_path` in project Files, up to 32,000 bytes. A single
explicit Website/Product URL/Site entry in project memory can supply the URL. Ambiguous
memory does not choose a website silently. At least one source is required before start.

The worker pins the project revision and source selection before compute. The GitHub
gateway provides a commit-pinned snapshot and its completeness metadata; credentials
never enter the sandbox. Source code is evidence only, kept separate from project state.
A local coding agent can instead curate attributed observations and useful excerpts into
the source packet. Capture does not read the caller's local files automatically.

The package uses the browser profile for browser inspection, screenshots and computed
styles. It does not require Studio's image or voice services. Its session is bounded to
900 seconds with the ordinary protected API runner, admission and verified usage billing.
There is no extra classifier/model route and no measured live cost estimate yet.

Instructions call for the homepage and one relevant deeper page, including measured 1440×900
and 390×844 viewports and screenshots. The latter tests responsive Firefox layout, not mobile
Safari or touch behavior. Screenshots are bounded tool results, not extra durable project files.
Camoufox masks its Font Loading API; an `error` status alone is not evidence of failed loading.
Capture checks resource transfers, network/decoder errors and visual evidence separately from
CSS declarations. Ambiguous rendering remains unverified. A narrower sample needs an explicit reason. URL-only capture
documents what was visible; it cannot assert unseen application components or states.
Repository facts cite exact commits and paths. Supplied observations retain their attribution.

## The two documents

| File | Purpose | New document sections |
| --- | --- | --- |
| `brand/BRAND.md` | Guidance for future marketing, including imagery vocabulary, protected choices and bounded supporting rules. | Brand direction; Visual style; Generation rules; Assessment and sources. |
| `DESIGN.md` | Evidence about the existing product/site design, with gaps and contradictions explicit. | Visual foundations; Components and patterns; Screens and flows; Constraints and evidence. |

The assessment is advisory. Identity class and evidence status remain in a bounded JSON
block; prose explains observations and implications. There are no invented probabilities
or public quality badges. Weak execution never authorizes replacing a founder's color or
font. `develop` permits modest supporting guidance for future assets within the supplied
notes; it does not authorize changing the product. Jev is outside this release.

BRAND.md ends with the small `tin-brand.v1` token block. New documents are validated for
structure, bounded JSON, complete palettes, palette/prose consistency and declared source
references. This proves document shape, not taste or the truth of a model's observations.
The exact schemas and instructions live in the [package contract](../workflow_packages/brand.capture/skills/brand-capture/CONTRACT.md).

## Review and consumption

The run publishes two proposals under `brand/proposals/{run_id}/`, bounded to 48,000 and
64,000 bytes respectively. The existing reader links the companion, shows palette swatches
and describes each destination as new or carried forward unchanged. “Use documents” applies
the exact reviewed pair. The [reviewed-document harness](reviewed-project-documents.md)
provides atomic adoption, destination conflict checks and retry recovery. Editing either
proposal or destination during review requires a new decision; neither file is partially applied.

MCP discovery includes preparation metadata and `get_brand_guide(project_id)`, which checks
current files without a model call. Incomplete starts return that preparation path.
`get_brand(project_id, revision?)` and `GET /api/projects/{project_id}/brand?revision=...`
read only active BRAND.md at one immutable project revision, under project membership.
They return the guide, tokens, optional assessment and diagnostics. Missing and invalid
guidance are distinct. Invalid optional assessment does not discard valid brand guidance;
invalid core tokens do not fall back to a prior proposal or guide.

Consumers can use that same revision to read DESIGN.md. The diagram workflow now uses this resolver at its pinned checkout revision; see
[diagram branding](diagram-renderer.md#project-brand-guidance). Character generation, ads and video
do not yet consume the resolver. Palette swatches
are a review aid, not proof that those outputs preserve an identity. Human review of real generated outputs remains necessary to assess identity fidelity.

## Verification and activation

`tests/test_brand_capture.py` covers source admission, pinned preparation, exact preservation,
new document validation, membership, discovery, active-only reads and the normal
proposal → review → atomic adoption → success path. Harness tests cover retries and races.
Browser fixtures cover both reader and Decisions approval, including safe palette swatches.
The [qualification cases](../workflow_evals/brand.capture/qualification.json) specify three
synthetic identities and a human rubric; they have not been run through a live model.

Source registration is not deployment. Catalog sync and an updated browser image with
`TIN_PROCEDURE_DOCUMENTS_V1` support are required before hosted runs. No live website/model
acceptance, model-cost measurement or deployment was performed for this slice.
