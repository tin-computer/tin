# Tin diagram renderer

Tin owns the visual layer for the existing workflow diagrams. The Eraser trial was
useful for comparing automatic layout, spacing, and editing, but its separate canvas
and built-in typefaces added another representation to maintain. Registry metadata
and project Mermaid files remain the editable sources; there is no Eraser sync or
separate PNG/SVG publishing pipeline.

The shared renderer applies to Registry setup panels, `.mmd` files, and Markdown
embeds. It preserves the seven node kinds, two edge kinds, existing workflow
definitions, source/ASCII views, and artifact approval behavior.

## Where to change it

- `web/diagram-renderer.js`: node dimensions, typography roles, SVG shapes, routes,
  and connector clearances. `renderFlow` and `renderSource` return promises;
  parsing, canonical source compilation, and ASCII remain synchronous.
- `web/diagram-contract.js`: bounded parsing and source compilation.
- `web/diagram-composition.js`: measured group arrangements and candidate selection.
- `web/diagram-alignment.js`: bounded cross-axis translations of whole subtrees.
- `web/diagram-ports.js`, `web/diagram-routing.js`: deterministic attachments and
  the libavoid adapter; `web/diagram-labels.js` reserves readable label corridors.
- `scripts/build_diagrams.mjs`: builds the browser bundle and copies its separately
  loaded `diagram-routing.wasm` asset.
- `src/tin_lite/diagram_compositions.py`: publication validation for `tin-diagram.v2`.
- `src/tin_lite/static/app.css`: Paper light/dark tokens, packaged font declarations,
  and surface sizing. Diagram font aliases isolate these faces from shell typography.
- `web/diagram-font-metrics.js`: advance widths from Geist Sans Regular/Bold and vertical
  metrics from all four packaged faces. Geist Mono uses its 0.6em advance. Font changes must update the metrics
  with `scripts/build_diagram_font_metrics.py` and rerun the browser checks.
- `src/tin_lite/catalog.py`: existing `presentation.flow` definitions. These remain
  descriptive metadata; styling changes do not change workflow execution.

The visual reference is the `thinklikeanagent` Paper file, boards 49, 50, and 51,
with the light/dark color system from boards 03 and 104. Labels use Geist Sans;
facts and human-gate labels use Geist Mono where the vocabulary specifies it.
Regular and Bold are the only packaged weights. All four faces are OFL-licensed
and load when used. They do not depend on fonts installed on the viewer's machine.
Hosted shell/auth screens separately load FK Grotesk Neue from Cloudflare; that
licensed face never enters portable diagram exports. See `font-serving.md`.

## Layout and connectors

For flat flows, the renderer calls the pinned ELK 0.11.1 public async API with dimensions computed
from Tin's font metrics. This is the same layout kernel previously used through
beautiful-mermaid; it is a visual layout dependency, not a workflow execution engine.
Beautiful-mermaid remains responsible for flat ASCII rendering. Compositions use an ASCII
hierarchy plus a complete connection list; the dependency cannot reliably render every nested
return path. Tin's finite dialect is
validated before either renderer receives it.

Nodes use an 8px size grid, 8px corners, 20px horizontal text padding, and a separate
16px lane for semantic marks. Names and facts wrap independently within 224px;
word wrapping balances the lines and overlong tokens break at grapheme boundaries.
The original logical lines remain intact in Mermaid and accessible labels. Type is
never condensed, truncated, or scaled to fit. Baselines use the packaged fonts'
vertical metrics, with 4px between wrapped lines and 6px between name and fact.
Height grows with content, reserving at least 13px above/below the text block before
grid rounding. Ordinary two-line cards share a 64px height across the different
type roles. Storage reserves another 12px for its cap rather than squeezing text
under the ellipse. Waits use an open ring, human gates use bermellón, and receipts
use moss. Steps, execution surfaces, and storage share the same neutral card fill
and border. Storage is distinguished by its cylinder silhouette; its cap uses the
same fill as its body. Enclosing groups retain a subtle wash. Neutral shades do
not encode status or execution role. Colors resolve from the host's theme on
every paint.

Connectors start 6 diagram pixels after the source boundary. Arrow tips end 10px
before the target boundary. Shaft and head share one opaque, 1px stroke: `#929087`
on paper, `#7d786f` on coal. Separate alpha values caused the original mismatch;
even matching translucent strokes can darken where they overlap. The opaque shared
token prevents that seam. Each arrow is an unfilled open chevron with rounded ends
and joins, 4.5px long and 6px across (about a 67° opening). Each is a local
polyline, so multiple inline SVGs cannot collide through shared marker
IDs. Trimming walks across short segments and duplicate route points. ELK handles
branches and return paths; no hand-positioned anchors are stored. Cycles retain
the authored node progression, so a revision signal does not move the draft after
its review gate. Adjacent layers have at least 48px between them, sibling nodes
32px. Connector labels wrap within 160px and reserve 10px horizontal/6px vertical
padding; the layout engine receives these full dimensions before routing.

Surfaces show diagrams at their natural size and scroll when needed. They preserve
readable type at phone width and keep both ends reachable. Flat diagrams retain their two-to-eight-node contract. Larger source compositions use the
bounded group grammar below; neither form adds a visual workflow editor. Latin font advances are measured; other scripts use
conservative fallback widths and should receive visual review.

## Verification

Run `node --test web/diagram-renderer.test.js`, `npm run build:diagrams`, and
`npm run test:diagrams-browser`. The browser check combines the three real catalog
flows with 28 fixtures from `web/diagram-fixtures.js`: all primitives, short labels,
maximum wide/narrow text, long prose, fan-out/fan-in, review loops, waits/signals,
long connector labels, cycles, Unicode, twelve-edge graphs, and eight-node chains.
Every flat fixture runs in LR and TD. Seven canonical reference compositions in
`docs/diagram-studies/` add nested containers, folded sequences, parallel recipients, cross-lane
signals, self-loops, and memory feedback. Three further composition fixtures test a
31-destination fan-out, routing around unrelated nested frames, and a consecutive
review chain with a side outcome. The 41 examples run at 1440px and 390px, in both
themes, in all three product surface containers: 492 rendered cases.

`web/diagram-audit.js` measures real SVG text after fonts load. It checks padding
inside the cylinder body as well as cards, balanced vertical space, line separation,
mark clearance, 6/10px connector gaps, matching opaque shaft/head paint, labels
covering other routes or tips, node collisions, frame shading, orthogonality, tiny
interior jogs in compositions, and clipping. Surface checks cover
natural sizing, reachable horizontal scrolling, page overflow, theme switching,
font loading, and style isolation. The actual async Markdown mount and invalid-source
fallback are exercised too. Unit checks preserve canonical source and accessible
labels while wrapping, the authored progression through cycles, syntax rejection,
repeatability across concurrent requests, straight reciprocal connections, and
foreign-frame avoidance. Chain regressions cover a common connector axis in all four
directions, edge-order independence, and fallback for signals, turns, ambiguous
branches, and blocked corridors. Browser checks also exercise a failed WASM fetch followed
by two concurrent retries, and ensure flat flows never fetch that asset.

For visual review, run:

```sh
npm run build:diagrams
TIN_DIAGRAM_SCREENSHOTS=/tmp/tin-diagram-qa npm run review:diagrams -- /tmp/tin-diagram-review
```

Open `/tmp/tin-diagram-review/index.html`. This local gallery switches themes and
filters orientation, with every diagram exported as an SVG containing its fonts
and resolved colors. Two overview SVGs collect the real workflows. The command
also verifies the exported Geist face is actually used, decodes every gallery image,
and exercises both controls. Screenshots at 2× and geometry reports go to the QA
directory (defaults to `qa/` under the output directory). It exits nonzero when an
audit fails but still leaves the previews available for inspection. These are
derived local review files, never canonical product artifacts or committed exports.
`npm run check:diagrams` additionally verifies that the bundle matches its source.

The September 10 review compared the baseline with two layout/type iterations,
then normalized ordinary two-line card heights across font roles. The review caught
cap crowding, mismatched arrow paint, cramped edge-label padding, stranded words in
wrapped facts, and a dense review loop that had reversed the main progression.
Passing geometry checks complements visual inspection; it does not establish that
every possible graph or fallback font will produce an equally good composition.


## Files diagram navigation

The Files diagram viewer opens with the complete SVG centered inside the available
window, using both width and height and 24px of surrounding space. Small diagrams
start at natural size. The context bar, Diagram/Source/ASCII selector, and zoom
controls stay outside the canvas, so a tall diagram cannot push navigation below
the screen. Source and ASCII scroll within that same bounded panel.

`src/tin_lite/static/diagram-viewport.js` supplies a camera over the rendered SVG.
It does not alter layout, fonts, source, or export geometry. Controls provide zoom
in/out, a percentage readout, actual size (100%), and Fit. Drag or a two-axis
trackpad scroll pans; pinch or Ctrl/Command + scroll zooms around the pointer.
With the canvas focused, arrow keys pan, +/− zoom, 1 selects actual size, and
0/Home fits. Browser zoom shortcuts outside the canvas are untouched.

Fit mode follows resize. After manual navigation, resizing preserves the current
scale and focal point where the diagram bounds permit. Bounds keep the drawing
reachable; zoom is capped at 400%, with a lower limit that still allows any
supported composition to fit. The current camera belongs to the open file route:
switching to Source/ASCII and back retains it, while reopening a file starts with
an overview. Navigation registers no project writes or approval effects. Cleanup
releases pointer capture, listeners, and the resize observer on leaving the view.

`npm run test:diagram-viewport` exercises the actual packaged Files route with
small, very wide/tall (32-node), nested, feedback, and review-chain fixtures, in
both themes at desktop, tablet, portrait-phone, and landscape sizes. It checks
full bounds, unobstructed controls, keyboard/mouse/native touch navigation,
resize, camera retention, zoom limits, source/error recovery, and zero writes.
Set `TIN_VIEWPORT_SCREENSHOTS=/tmp/tin-viewport-qa` for screenshots. Registry and
Markdown embeds retain their current scrolling behavior; the camera is mounted
only by the Files diagram viewer.

## Compositions and publication versions

`tin-diagram.v1` and `tin-diagram.v2` remain registered for historical pinned runs.
`content.diagram` v2 uses `tin-diagram.reviewed.v1`: the v2 composition grammar plus
the bounded render/inspect/repair and independent publication checks described below.
Registry `presentation.flow` retains its original small contract.

## Per-run review and publication

`node scripts/check_diagram.mjs check candidate.mmd --out /absolute/temporary-directory`
validates arbitrary candidate files, not just gallery fixtures. It runs the packaged
product renderer and real fonts in Chromium, fulfills all browser requests from an
in-memory resource allowlist, and audits light and dark themes. JSON diagnostics bind
the exact source bytes to a SHA-256, renderer/font/CSS fingerprint, and browser version.
The reusable font/export helpers also power `review:diagrams`.

The procedure controller supplies the actual overview images in a follow-up Codex
turn. Larger compositions also receive natural-size detail tiles for the image viewer.
Acceptance must name the source hash that was inspected. A changed source requires a
new render and inspection. Grammar/layout failures and semantic revisions share a
maximum of three candidates (initial plus two repairs) and the existing run budget.
Geometry checks cannot establish meaning: the procedure explicitly checks the brief,
source evidence, hierarchy, return paths, and uncertainty during visual inspection.

The pinned project revision is checked out before generation. Cited project paths,
revision, and content digests are captured in the existing execution result/rollout
evidence; successful ordinary persistence keeps them in its receipt. A recovered
checkpoint never fabricates a missing model inspection narrative or source list.

Before canonical publication, the trusted activity re-renders the exact checkpoint
bytes in a fresh, short-lived sandbox with no credentials and all egress denied. This
same check runs on checkpoint recovery, without repeating a paid model call. Its proof
is stored in the existing persist receipt and checked against the immutable checkpoint
again at publication. Only `.mmd` is saved, with the usual conflict/review behavior.

Checker, fonts, renderer, WASM, and Playwright 1.63.0's exact Chromium build are
installed in the default/isolated E2B image; runtime downloads are forbidden. Trusted
packaged product CSS is refreshed before execution, so unrelated shell color changes
do not require a compute-image rebuild. The open-egress browser/Studio profile is not
used for diagram checks. Tests: `node --test scripts/check_diagram.test.mjs` and
`uv run pytest tests/test_diagram_review.py`, alongside the renderer/browser suite.

Reviewed diagram API runs pin an image-capable variant of the existing v3 transport
contract: at most 8 MiB per request, with non-image context still capped at 1 MiB.
Only inline PNG/JPEG/WebP `input_image` data receives the extra byte allowance.
The 128k context window, cumulative token limit, request count, and quoted spending
ceiling are unchanged; supplier-reported image tokens follow normal accounting.
Historical diagram grants and ordinary text procedures keep their original limits.
This is needed for both-theme overviews and natural-size detail tiles across repairs,
not a larger text context or a separate model execution path.

Composed `.mmd` files opt in with `%% tin:composition` after the graph header. Named `subgraph`
blocks carry a local `direction LR|RL|TD|BT` and `%% tin:group frame|lane|layout`. Frames show a
boundary, lanes show a heading, and layout groups arrange children invisibly. This is a finite
presentation grammar, not arbitrary Mermaid. It allows 2–32 nodes, 1–48 edges, 1–16 groups,
and four levels of nesting. Node names/group names are at most 80 characters, facts 240, and
edge labels 96. Every node belongs to the one ordered hierarchy; isolated informational nodes
need a meaningful group, not an invented causal link. Edges connect leaf nodes only. Duplicate
IDs, endpoints, unsupported syntax, and executable content fail closed. Browser/server parity
checks exercise the same reference sources and rejected inputs.

Composition placement measures children recursively and arranges rows/columns using
their own directions. Sibling spacing accounts for crossing edges and label dimensions.
Frames have 24px padding, a reserved title area, and a subtle `--paper-task` fill;
lane headings reserve their own space. Text uses the same shared primitives as small
flows. A labelled self-loop reserves enough node width for its return segment. Nodes
with more than eight incident connections grow vertically to make room for separate
attachments, keeping the text centered at its normal size.

The composition router uses pinned `libavoid-js@0.5.0-beta.5`. Its 492,029-byte WASM
loads lazily from the same asset directory as the renderer, with shared initialization
and retry after a failed fetch. Exported SVGs contain no WASM or runtime dependency.
The package license, source, and replacement instructions are in `THIRD_PARTY_NOTICES.md`.

Tin selects endpoint pairs deterministically, preferring a shared axis over independently
centered ports. Attachments stay 12px from corners and 16px apart. The native router
finds obstacle-free orthogonal paths with a bend penalty and separates neighboring
segments. There is no second custom path-search algorithm. Node bodies and group
headings are obstacles; unrelated frames are treated as outer boundary obstacles,
without duplicating their nested obstacles. Reciprocal edges retain distinct endpoints.

Consecutive near-aligned calls also prefer one common axis across the whole chain.
Eligible neighbors have centers at most 24px apart on the cross axis; each interior
node must have exactly one eligible incoming and outgoing call in the same geometric
direction. Side branches can stay independent, while signals, reversals, self-loops,
and ambiguous forward forks do not join the chain. The common axis must fit every
node's inset range and have a clear corridor. Its preference costs less than one
additional bend, and normal obstacle avoidance and occupied-port spacing still win.
This changes attachment choices, not node placement or source semantics. It addresses
the case where individually straight connections drift sideways through successive
nodes because each pair chose a different midpoint. If no common corridor exists,
ordinary pairwise planning remains available. This is deliberately local and bounded;
it does not force all branches or widely separated nodes onto a single line.

After routing, labels avoid all other paths, nodes, titles, labels, and arrowheads.
A crowded label may sit beside its path. If that still fails, a bounded exterior
corridor expands the canvas and reroutes the connection. Native objects are disposed
per composition; route points are borrowed and copied into plain JS coordinates.

The original three compositions remain the baseline: the measured arrangement, a bounded center
alignment, and an alignment informed by the first routing result. A move translates a
whole subtree along its parent's cross axis. Authored order, sibling spacing, frame
padding, framed child alignment, and lane headings remain intact. Candidate ranking
considers bends, short segments, crossings, route length, and canvas area. Invalid
candidates are rejected; an unsuccessful refinement cannot replace a valid baseline.
This is a small deterministic refinement, not a global layout optimizer.

Three additional candidates center equivalent leaf branches, compact the measured arrangement,
and combine the two. Equivalent peers have identical directed, typed external connections;
their hub aligns with the visible envelope, preserving the spacing between unequal-sized cards.
Compaction reserves a label's full size only between its adjacent endpoint regions rather than
at every cut crossed by a distant return. It keeps at least 72px between siblings and reroutes
through the same obstacle and label checks. It neither repacks rows nor changes authored groups.
New candidates cannot add crossings, short jogs, or measured chain drift, increase total route
length by more than 5%, or reduce fit scale at 1200×800 by more than 3%. Surviving candidates use
the original score plus preferences for continuous axes and balanced equivalent branches.
There are at most six arrangements; there is no open-ended search or new pathfinding engine.

The offline checker supplies advisory composition metrics alongside the hard geometry audit.
These measure dimensions, fit scale, bends, crossings, short jogs, route length, longest detour,
local chain drift, and branch imbalance. They do not infer the intended main path or prove
semantic quality. A diagram can pass geometry and still be a poor explanation. The generation
skill and image-inspection turn explicitly require main-path planning and overview/detail review.
See [implementation, comparisons, and engineer handoff](diagram-composition-quality.md).

The seven screenshot reconstructions are visual reference studies, not assertions about the
current deployed architecture. Their canonical files live in `docs/diagram-studies/`; the JSON
manifest supplies gallery titles and context only. The review command exports both themes with
embedded fonts. Reviewing three initial passes caught needlessly long label detours, inconsistent
lane alignment, and nearly coincident feedback routes. The revised router keeps local paths
local, aligns lane headings, and separates nearby parallel paths. These examples are regression
cases, not a guarantee that every allowed topology has an ideal automatic composition. Source
hierarchy remains the author’s tool for changing the reading order.


## Project brand guidance

`content.diagram` 2.2 uses `tin-diagram.branded.v1`, an extension of the existing reviewed
composition contract. Before compute, the trusted activity checks active `brand/BRAND.md`
and optional `DESIGN.md` at the immutable project checkout revision. The agent reads those
files for identity, composition guidance and product language. Unadopted proposals are not
sources. A missing brand keeps Tin's appearance; invalid active guidance fails explicitly.

One bounded `%% tin:brand` JSON comment immediately after the graph header carries the
brand file's revision, SHA-256 and exact approved light/optional dark palettes. Publication
rejects a missing or changed snapshot. The existing source hash and independent offline
render check bind the palette along with the diagram; retries resolve the same project
revision. No new receipt table, artifact companion or workflow engine is required.

The renderer scopes palette variables to that SVG. Labels use brand ink, neutral surfaces
and borders derive from ink/paper, and evidence marks use the accent. Human gates keep their
labels and geometry rather than depending on color alone. A light-only brand keeps its
paper in dark UI; an explicitly supplied dark palette follows the reader theme. Other
figures, the Markdown reader and Registry diagrams retain Tin styling. Exported SVGs carry
resolved colors and embedded bundled fonts and do not need a live brand lookup.

This slice retains measured Geist Sans/Mono. Font names in a guide do not silently substitute
unavailable files. Repository font acquisition and measured custom-font layout remain separate
work; no customer font binary enters this source distribution. The diagram source stays
editable, and the usual human review remains in place. Historical validators keep their
original contracts.

Rebuild the isolated image and deploy it with the renderer before catalog activation. Branded
runs check the image's `--brand-version` capability before paid execution. Fixture checks cover
active-only resolution, revision/digest/palette binding, malformed or injected metadata,
legacy parsing, multiple brands together, light/dark switching, Markdown embeds, offline
checks and portable SVG export. These checks do not establish model quality or font fidelity.
