# Writing style capture

`style.capture` is a manual native workflow in Organic traffic. The user's coding agent curates
selected evidence; Tin extracts an editable guide through the existing model service. Guide contents remain in project Files; there is no
sandbox, background history scan, or second execution engine. Calling the
project-authorized MCP `get_writing_style_guide(project_id)` or matching HTTP guide endpoint remains
read-only. Only an ordinary authenticated workflow start buys the extraction.

Ask the intended publishing context and which samples represent it. Offer selected notes, articles
or accessible session passages; never sweep whole histories or treat assistant replies as the
user's prose. The caller commits one ordinary `style/sources/<name>.md` source packet using the
guide's template and `commit_project_changes`. Samples carry unique IDs, readable origin labels,
exact selected text, and a kind: authored, note, conversation, correction or reference. Project
references include their revision; local samples omit private absolute paths. The packet contains
only material the user elected to share with project members. Preferences-only input is valid.

## MCP source discovery is part of the workflow experience

`get_workflow` and the full `list_workflows` listing (`detail="full"`) expose a `preparation`
next call for the built-in `style.capture`: `get_writing_style_guide(project_id)`. Attempting a new start or saved
configuration without a source path returns `style_sources_required` with that same next call,
before creating a run or configuration. A prepared packet remains an ordinary, retryable input;
there is no new consent table or activation gate. The dashboard's copied prompt starts the same
conversation.

The detail response also includes the complete guide in `preparation.guide`. Existing MCP
sessions with a cached tool list can use their already-known `get_workflow` without reconnecting
to discover the newer dedicated tool. The catalog list keeps only the compact next-step metadata.

The guide (`writing-style-guide-v2`) tells the caller to:

1. Establish the intended audience, language and writing format. Offer authored articles/drafts,
   Obsidian notes and recent Codex, Claude Code or other harness sessions explicitly.
   Ask specifically for blog posts the user wrote and still likes, accepting links or files;
   blog samples should not be left implicit under "articles" or other sources.
   Use one short invitation, skipping it when samples are already supplied. The source families
   are a menu, not questions to ask one by one: sufficient blog posts do not trigger additional
   requests for a vault and session history. Reuse known context and ask a focused follow-up only
   for a consequential gap; combine selection review and sharing/run confirmation once.
2. Obtain scope to inspect a selected folder or a relevant session range, honoring permission
   already given. Help locate sources with the caller's available tools. Inventory metadata
   before reading likely candidates; do not require users to export already-readable notes.
3. Curate substantial original passages, distinguishing published prose, developed notes and
   conversational editing preferences. Prefer several independent pieces, often 1,500–4,000
   words together when available; this is an editorial target, not a minimum validation gate.
   The eight-sample upload limit is not an eight-candidate discovery limit.
4. When using session records, identify authors and deduplicate repeated records. Exclude
   assistant/tool text, injected instructions and compaction summaries as evidence of authorship.
   Use the installed harness's tools or documented local format rather than assuming one storage
   path. If access is unavailable or declined, explain that and offer selected exports or writing.
5. Preview the chosen passages and their contribution, gaps and exclusions. One confirmation
   can cover sharing that selection with project members and running capture. Do not ask again
   for each file or make users select versions.
6. Show the resulting guide and demonstration, and invite corrections before claiming it captures
   the user's voice. Thin evidence can be a deliberate choice, but a provisional label does not
   justify skipping discovery or silently substituting a few current-chat messages.

This is a client-led conversation, not a claim that Tin can read another application's history
or attest to permissions granted outside Tin. The server enforces membership, packet validation
and ordinary run boundaries; it does not invent a server-verified consent boolean. The guide's
source template is deliberately empty and invalid until populated with actual evidence or
explicit preferences, rather than supplying runnable example preferences.

When Tin refuses a source packet, at start or when the run reads it, the message names the file
and what is wrong with it: a path Tin cannot read, not Markdown, missing at the revision, empty,
over 100 KB, binary, not UTF-8, off the template, something that looks like a credential, or the
first field problem in the samples block. The message never repeats the file's contents.

## Hosted extraction

Start `style.capture` through the same HTTP/MCP run service with `source_path` and optional
`direction`, reusing a stable start request ID on retry. Before the model call, the activity pins
the packet, existing guide, project revision and immutable extraction contract. One structured
model call yields concise source-backed rules and an original demonstration. Deterministic code
rejects nonexistent source IDs and bounds the output; conversational/reference-only evidence is
explicitly provisional. Stated preferences remain verbatim, with later direction taking precedence.

Capture 1.1.0 proposes the guide before it takes effect, the way brand capture proposes its
documents. The run saves the extracted guide to a run-owned file,
`style/proposals/<date>-writing-style-<run>.md`, and waits in Decisions. The active guide stays
unchanged until a member approves. Approval binds the proposal as it stands at that moment,
including edits made to it in Files, and only then is it saved as the guide. A proposal removed or
emptied before approval fails the run and leaves the current guide alone. "Discard" on the
Decisions card turns the proposal down instead: the run ends as declined, the proposal file stays
readable in Files, and the current guide is unchanged. Runs pinned to 1.0.0 save the guide
directly, as before.

Capture 1.2.0 drops edits in Files as the way to correct a waiting proposal (Emre, 10/1:
Decisions keeps Approve and Discard; the coding agent revises the material). The agent calls
`get_workflow_review` for the `review_token`, then MCP `revise_capture_proposal` (or
`POST /api/workflows/runs/{id}/proposal-revisions`) with the complete revised guide. Tin
accepts it only while the run waits, only for that run's proposal file, and only if it keeps
the captured guide's shape: the `writing-style` front matter, the title and every section in
order, at most 24,000 bytes, with no credentials. It commits the text over the proposal and
records who sent it. Decisions shows how many times, by whom and when the guide was revised.
Approval stores the SHA-256 of the exact version the founder read and saves that version; a
revision after they opened the card refuses the approval and shows the new version. Direct
edits to a waiting proposal are refused with a pointer to the tool. Runs pinned to 1.1.0 keep
approving the proposal as it stands, edits in Files included.

The result is `.agents/skills/writing-style/SKILL.md`. The same canonical publication mechanism
used for saved procedure outputs checks whether that destination changed since capture began.
An unrelated file edit is fine; a concurrent guide edit preserves the current file and retains
the extracted guide for the existing comparison page. A lost publication response is reconciled
before any replacement. Native checkpoint creation has its own run-owned ephemeral branch and
receipt, not sandbox credentials. No model repurchase occurs on retry after an uncertain request.
Publication, Activity and the terminal Postgres projection are idempotent; Temporal carries only
the run ID. The model service records usage through its existing accounting boundary.

Users edit the guide directly in Files or with their agent; future article runs read it without
activation or a version picker. Directly saving a complete agent-extracted guide remains supported
for callers who do not want hosted extraction.

## Several authors in one project

Capture 1.4.0 optionally accepts `author_id`. Dashboard author choices come from current
project membership and show Clerk name/email labels, defaulting to the signed-in member.
There is no manual author creation form. Capture retains the explicit shared-project guide
choice for workflows that use it.

`list_project_authors(project_id, members=true)` lists these choices without writing records.
`select_project_member_author(project_id, member_clerk_user_id)` reserves a guide binding once
or reuses the member's existing ID and guide. HTTP uses the same services through the authors
listing and `POST /api/projects/{project_id}/authors/member/{member_id}`. Membership is checked
before profile lookup and again under the project lock at selection. Clerk profile labels do
not grant access or establish authorship. Historical unlinked author records and saved runs
are retained; they are not offered as new dashboard choices.

The original binding API remains compatible for existing callers. `list_project_authors` and
`save_project_author` (also available at `/api/projects/{project_id}/authors`) maintain
small project-scoped bindings: stable ID, display name, optional member, guide path and
version. The authenticated member can explicitly link themselves; names never establish
identity. New capture setup defaults only to that explicit member binding. Switching authors
keeps unfinished samples separate. The shared-project option remains explicit.

Create with a caller-generated UUID and `expected_version: 0`; edits use the listed version.
A missing `selected_guide` reserves `.agents/skills/authors/<id>/SKILL.md` for a new author,
or preserves an existing binding. Supplying a guide explicitly checks that it is a usable
Markdown file. A guide cannot belong to two authors. Copy a shared project guide to a separate
file before associating it with one author. Omitting `link_to_me` preserves the existing link;
`true` links the caller, and `false` removes only the caller's own link.

`get_writing_style_guide(project_id, author_id)` returns preparation instructions and the
selected destination. Start capture with that same `author_id`; admission pins the binding
in the existing receipt transaction before compute. Proposal edits and exact-version approval
use the existing review service. Publication and retained-output comparison target the pinned
file. Another author's guide and the shared guide remain untouched. A binding cannot change
destinations while its capture is active. Renaming the author does not redirect an admitted run.

Omitting `author_id` preserves shared-guide behavior. Historical definitions retain their
original policy, destination and output cap. These records do not grant membership or posting
permission. Existing drafting workflows are not automatically converted to author consumers;
the readable drafting setup and per-draft documents are separate follow-up work.

## Dashboard fallback

Setup leads with a copyable coding-agent prompt and the existing connection instructions. The
secondary inline sample section supports pasted passages, existing project Markdown/text files,
and MD/TXT/DOCX uploads. Uploads use a membership-gated, no-store text-preview endpoint; original
binaries are not retained. The selected text is previewable/removable before the normal Files
commit and workflow start. In-memory drafts are project-scoped and never stored in localStorage.

Limits: 8 samples, 100 KB packet, 24 KB guide; 5 MiB per uploaded file and 10 MiB per selection.
DOCX parsing is bounded and reads main-document text only. Hidden text, comments, headers and
footnotes are excluded; tracked changes require a clean export. PDF and legacy DOC are deferred.
The UI reuses the existing template card, field, button and agent-connection patterns.

`content.public_article` 1.3.0 declares `style.capture` as the producer of its recommended guide
and joins the Organic traffic catalog group. The optional skill reader introduced in 1.1.0 is
unchanged. Explicit run voice notes
win over its style preferences. Neither can override factual grounding, output ownership or review.
Existing saved configurations retain their pinned definition; this release does not silently
upgrade them. The generic article remains a reviewable project draft, not a site publication.

## Remaining content-generation slice

The dedicated plan-to-draft workflow is not implemented by this handoff. It should select one
non-deferred item from an exact same-project content-plan revision; pin the selected brief,
source references and style revision before paid work; retain verification requirements; write
an individually addressable draft; and expose review without changing the future roadmap or
pretending that pending factual checks are complete. Both MCP and the schema-driven dashboard
must use the same ordinary run service. Start with one brief, not an automatically launched batch.

GitHub delivery should take an explicitly selected reviewed draft and repository destination,
verify the repository's content/build conventions, and create an unmerged PR through the existing
integration gateway. Connecting GitHub alone must not publish or start this step. There is no new
engine, style database, activation lifecycle or custom style editor in this slice.
