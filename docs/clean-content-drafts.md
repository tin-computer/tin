# Clean articles, separate generation notes

## In plain English

The writing workflow produces an article for readers and a separate internal note for its
owner. Reviewing the article is not an invitation to provide API keys or run a live product
test. Website adaptation/build checks belong to delivery; optional product QA is later work.

## Output contract

New `content.generate` 1.3.0 definitions use `content-draft.v2` on the existing procedure
executor. They write exactly two bounded, ordinary UTF-8 Markdown files:

- `content/drafts/{run_id}.md`: public copy, starting with its title. No Tin frontmatter or
  generation checklist. Existing article approval and optional delivery still apply.
- `content/drafts/{run_id}.generation.md`: source provenance, evidence/style basis, and
  concise accounting for the brief's requirements. Maximum 24 KB. This is a supporting
  document, not another decision and never a website-delivery input.

The article reader exposes a quiet **Generation notes** link through generic related-document
context. MCP `get_run` returns the same path and pinned revision. Notes open through the normal
membership-gated Files reader without an approval action.

Missing factual support still requires narrowing, qualifying or omitting claims. Public docs,
supplied files and local syntax/mocked checks can support a draft; none proves live behavior.
The workflow does not request/use product credentials, create accounts, register integrations,
send real messages or test activation. An older brief asking for live QA gets an optional
`follow-up` entry, explaining that it was not performed and how the article avoids relying on
it. No additional QA workflow, credential UI or blanket verification gate is introduced.

## Structure for search and answer engines

`content.generate` 1.7.0 and `content.public_article` 1.5.0 pin a `search-and-answer-engines`
skill adapted from the answer page's `10-search-and-answer-engines` guidance: an answer-first
opening of 40 to 60 words, headings phrased as the reader's next questions, a comparison table
when options are compared, an FAQ where it fits and `## Sources` last, with inline citations
and short paragraphs. It is guidance for the model, not a code check: the answer page's
structure checks, its `Last updated` line and its repair call stay with `content.answer_page`.
Updates keep their destination's structure and apply the rules only to changed sections.

A public article starts with a search listing, `meta_title` and `meta_description` as quoted
one-line frontmatter, then its title. `public-article.v2` accepts that listing, still requires
the title, and refuses a listing that delivery could not merge (other keys, lists, empty or
multi-line values, invalid YAML). Delivery merges it into the one site header, as it does for
answer pages. Planned drafts keep a title-first file with no listing: their delivery paths
(`render_file`, the approved-article source for `content.deliver`) do not yet split a draft's
own frontmatter.

New `content.plan` 0.5.0 definitions use editorial policy v4: drafting-time verification lists
contain desk checks, not mandatory live tests. Historical v1/v2/v3 planning contracts remain
supported exactly; existing roadmaps are not rewritten.

## Persistence and compatibility

Both files are checkpointed in the same ephemeral revision, validated before acceptance and
published in one existing fenced, CAS-guarded commit. A version-2 output checkpoint binds the
primary and one companion's bytes and digests. Version-1 single-file receipts serialize exactly
as before. Lost acknowledgments reconcile both files without repurchasing a model call.

Concurrent changes to either destination retain both generated files. Applying a saved primary
can bring along its unchanged companion, but never silently overwrite edited notes; that case
offers Keep current only. No new table, Temporal command sequence or workflow engine is added.

Pinned `content-draft.v1` drafts retain their combined format and existing delivery stripping.
Old approvals and pending decisions are not changed. New clean articles and historical combined
articles are both supported by the trusted Markdown and repository-aware delivery paths.

## Deployment and acceptance

Both default and isolated procedure images require the companion capability probe before
context transfer or model dispatch. CLI/model/auth contracts are unchanged. Browser and Studio
images are not rebuilt or selected by this slice.
