# Repurpose an article for social

`social.post_batch` reads an article from project Files and drafts two X posts and two
LinkedIn posts. It saves the batch in `reports/SOCIAL_POST_BATCH.md`, with an excerpt
supporting each draft. The normal review gate lets the founder read and approve the result.
Posting remains manual.

Supply `article_path` to use a particular file. Tin reads its current contents at launch;
there is no source-run ID or version to select. Without a path, the workflow looks for a
single article in `content/drafts/`, `content/articles/`, or the older
`reports/PUBLIC_ARTICLE.md` location. Generation notes are excluded. If several articles
exist, name the intended file; the workflow cannot infer chronology from UUID filenames.

`article_text` is an optional fallback when the named file is missing, or when no single
article can be chosen automatically. This is an ordinary string input, available through
MCP and the dashboard. An existing named file takes precedence. An unreadable, oversized
or malformed source does not silently become the fallback.

The workflow also reads the current `.agents/skills/writing-style/SKILL.md` when present.
All file reads use the project revision Tin records at admission. Later edits cannot change
this run or its retries; a new run reads the latest files again. Repeating a start request
ID returns the same run. The source may be written by a person, imported, or produced by
any workflow; the package does not claim it has been approved.

## Drafting and review

One managed `gpt-6-sol` call writes the four drafts. Python checks their platform order,
distinctness, length and supporting excerpts. Numeric literals and double-quoted phrases
must occur in the article. First-person claims and links are rejected. These checks do not
prove that every paraphrase preserves its source's meaning, so the result still needs review.

The package bounds X drafts to 240 UTF-8 bytes and LinkedIn drafts to 1,400 characters.
The full article and writing guide must fit the existing 32,000-byte model-request limit.
Oversized input fails before that request; no source is silently truncated. File reads and
validation happen before the model call, although a rejected input can still use sandbox time.
An invalid model result is not automatically purchased again.

Run batches sequentially: they share one output path. The existing publication guard can
retain an output conflict rather than overwrite a concurrent edit. Weekly planning,
used/held tracking, image generation and provider posting remain separate work.

## Existing saved workflows

Version 2 uses the file contract above. Saved configurations pinned to version 1 keep their
original `source_run_id` input, approved article and original writing-guide snapshot. Their
admission checks and receipt readers remain available; old runs are not reinterpreted.
Create a new configuration from the current Registry definition to use current project files.

`content.deliver` has its own exact approved-copy contract because it creates an external
GitHub PR. Changing how social drafts read an article does not change that delivery contract.

## Verification

Offline fixtures cover file precedence, caller fallback, ambiguous article discovery,
writing-guide reads, unusable input and invalid model replies. The shared
[project file reader](code-project-files.md) has admission, retry, project-boundary and
protected-channel tests. Legacy approved-source tests remain for saved version 1 definitions.

The opt-in `tests/test_social_post_batch_live.py` uses synthetic project files, a disposable
local database, real E2B and code.storage, and one paid managed model call. It requires explicit
provider authorization. Ordinary contributor tests use mocked providers; one successful
live batch does not establish editorial quality across customer articles.
