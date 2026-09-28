# Repurpose an approved article for social

`social.post_batch` turns one approved `content.generate` article into two X posts
and two LinkedIn posts. It saves the four drafts in `reports/SOCIAL_POST_BATCH.md`,
with a suggested order and an excerpt supporting each draft. The normal review
gate lets the founder read and approve the document. Posting remains manual.

Choose the article by title from `get_workflow` with a bound project. Its
`preparation.articles` list contains approved articles and their run IDs. Supply
the selected ID as `source_run_id` through the ordinary estimate/start flow;
the HTTP start endpoint accepts the same input. A GitHub connection, public
article URL and social calendar are not required.

Tin pins the article's approved revision before creating the run. It checks the
project, approval, publication proof and article bytes, and copies the original
writing guide from the revision used to draft that article when one exists.
Later edits to project files cannot replace either source during execution or
retry. A repeated start request ID returns the same run. A new request ID is a
new, separately metered batch, even when it selects the same article.

Run batches sequentially: they share one report path. If two runs generate at
once, the existing publication guard can retain the later result as an output
conflict rather than overwrite the first. Both model calls can still incur cost.

## Drafting and review

The package uses `workflow.code` with one managed `gpt-6-sol` call. Python checks
that the response has four distinct drafts in the requested platform order,
that each evidence excerpt ends as a complete sentence and exists in the article
(ignoring line wrapping), and that numeric literals and
double-quoted phrases occur in the source. It rejects first-person copy and
links, since article approval does not prove that a public URL is live.

X drafts have a conservative 240-byte UTF-8 limit; LinkedIn drafts have a
1,400-character limit. These are package limits, below the ordinary limits for
[X](https://help.x.com/en/using-x/how-to-post) and
[LinkedIn](https://www.linkedin.com/help/linkedin/answer/a522483/differences-between-posting-updates-and-publishing).
The complete source and writing guide must fit the existing 32,000-byte model
request bound. Oversized input fails before the model request; the package does
not silently truncate it.

These checks catch structural and some factual errors. They do not establish
that a paraphrase preserves a claim's meaning, or that the copy suits the audience.
Human review must check those points against the full article. An invalid model
response fails the run; it is not automatically purchased again.

This first version accepts only approved `content.generate` articles. It does
not accept arbitrary Markdown, `content.public_article`, transcripts or URLs.
Weekly planning, used/held tracking, image generation and provider posting are
separate work.

## Reuse in private code workflows

A code package can request the same source check with this optional declaration:

```json
"approved_article": {"input": "source_run_id"}
```

Put it inside `code`. The named input must be a required UUID field other than
`project_id`, and `schedule_modes` must be `["on_demand"]`. The executor passes
the saved source as `ctx["approved_article"]`, containing `article`, `title`,
`source_run_id`, `source_path`, `source_revision`, `source_sha256`,
`article_sha256`, and `style`. The first digest covers the published artifact;
the second covers the extracted article body. Style includes its path, revision,
digest and full `content` when present, or a null digest when absent.

The source resolver allows articles that also have GitHub delivery enabled.
Repurposing an approved article does not start or change that delivery. The
declaration does not grant access to other project files. Private activation,
membership, billing and execution limits remain the ordinary code contracts.

## Verification and release

The package is selected in source for the next catalog sync. This is not a
production deployment. Offline fixtures cover invalid model replies; disposable
database tests cover approval, project boundaries, admission races, immutable
source snapshots, repeat starts and one reviewed output under retry. The normal
code executor retains managed-call receipts, usage accounting and publication
conflict handling.

The opt-in `tests/test_social_post_batch_live.py` check has also passed with real
E2B, code.storage and a managed model call. It uses a synthetic approved article,
local Postgres and Temporal, and a synthetic identity and approval. It checks the
review gate, HTTP approval, durable output after sandbox deletion, duplicate
execution without another model call, and Temporal history replay. This checks
the execution path; it is not a deployed-service or customer-project acceptance.

To repeat it with authorized provider accounts and a disposable local database:

```bash
TIN_LITE_TEST_DATABASE_DSN=postgresql://test_user@127.0.0.1:5432/tin_test \
TIN_LITE_SOCIAL_LIVE_PROOF=1 \
  uv run pytest tests/test_social_post_batch_live.py -q -s
```

It reads only the E2B, code.storage and model credentials from `.env`, creates a
new test repository, and incurs provider costs. Temporal CLI must be installed.
It saves the synthetic source, resulting drafts and usage proof in pytest's
temporary output directory. The test repository is retained for inspection;
the sandbox and local database schema are cleaned up.

The [qualification cases](../workflow_evals/social.post_batch/qualification.json)
use synthetic source IDs. An explicitly authorized evaluation must select real
approved articles in its test project and review the resulting copy and usage.
The listed qualification cases have not yet been evaluated in a live Tin project.
One synthetic article smoke test does not establish quality across customer articles.
