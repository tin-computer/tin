---
name: error-pages
description: Inventory the error messages a product shows its users, find the ones no page explains, and draft the missing pages plus the link each error should print.
---

# Error pages procedure

Someone who hits an error copies it and pastes it into a search box or an assistant. That query
is the most specific one they will ever type, and the product's own code wrote it for them.
Most small products leave it unanswered, so the answer ends up in a stale forum thread or in a
competitor's docs. This procedure treats the codebase as the list of those queries: it finds
every message a user can read word for word, checks whether an answer exists, and writes the
missing ones.

Work through the stations in order. Each one has a gate. An item that fails a gate leaves the
line and is counted in the report, not silently dropped.

## 1. Harvest

Read `/home/user/project`. If `focus` is set, start there and harvest it completely before
anything else. Orient with the README, the package manifests and the entrypoints, then search
for the places a message leaves the program. Adjust the patterns to the languages you find:

- Raised or thrown errors: `raise .*Error\(`, `throw new`, `panic!`, `errors.New`, `fmt.Errorf`,
  `anyhow!`, `bail!`.
- Command-line output: `sys.exit(`, `click.echo(.*err`, `console.error`, `eprintln!`,
  `process.exit(1)`, `log.Fatal`.
- API responses: `HTTPException`, `status_code=4`, `res.status(4`, `res.status(5`, JSON bodies
  with `error` or `detail` keys.
- UI text: `toast.error`, `setError(`, error components, and i18n files whose keys contain
  `error`, `invalid`, `failed` or `not found`.

For every hit, record the literal message, its `path:line`, and the surface it reaches:
`cli`, `api`, `ui`, `sdk` (an exception a library user catches) or `log`. Replace interpolated
values with `…` and keep the literal parts exactly as written. Stop harvesting at 150 raw hits,
or earlier when the clock runs short; either way, say which directories you did not read.

The **anchor** of a message is its longest run of consecutive literal words, not counting the
`…` placeholders. On a tie, take the first run. An error code, if the message has one, is the
anchor instead. Every later station searches, merges and slugs by the anchor.

## 2. Gate: can a user search for it?

Keep a message only when all three hold:

1. **It reaches a person.** Surface is `cli`, `api`, `ui` or `sdk`. A `log`-only message
   stays only when the README or docs tell users to read those logs.
2. **It has a stable anchor.** The anchor is at least five words, or is a named error code
   such as `E1042` or `ERR_TOKEN_EXPIRED`. `Invalid input` and `Something went wrong` fail.
3. **It is not an internal assertion.** Messages that only fire on a programming bug, such as
   an unreachable branch or a type check the caller cannot trigger, fail.

Merge messages whose anchors are identical. Record how many were rejected at each check.

## 3. Coverage: does an answer exist?

For each kept message, search the repository's own public content (`docs/`, `website/`,
`content/`, `pages/`, `*.md`, `*.mdx`, and the site source if it is in the repo) for its anchor.
Assign one status:

- `linked`: the message itself prints a URL or an error code that a page explains.
- `documented`: a page explains this error, its cause and a fix, but the message does not point
  to it.
- `mentioned`: the anchor appears somewhere, such as a changelog, a code block or an issue
  template, with no explanation.
- `uncovered`: the anchor appears nowhere outside the code.

Only public content counts. Code comments and tests are not coverage.

## 4. Demand check: who answers it today?

Rank the `uncovered` and `mentioned` messages by provisional score, which is the score from
SCORING.md with `demand` taken as `not_checked`. Going down that order, for at most ten of
them, run one web search each with the anchor in double quotes. Record what the first page of
results holds, as one of:

- `none`: no result contains the anchor.
- `own`: the product's own issue tracker, forum or docs.
- `third_party`: Stack Overflow, Reddit, a blog or an assistant-generated page.
- `competitor`: a page run by a product that competes for the same user.

`third_party` and `competitor` both mean people already hit this error and someone else is
getting their visit. Cite the result URL. A search you did not run is `not_checked`; it is never
`none`.

## 5. Rank

Score every kept message with SCORING.md. Break ties by earliest point in the user's journey,
then by `path`. Do the arithmetic explicitly in the report so a reader can check it.

## 6. Draft pages

For the top `pages_to_draft` messages whose coverage is not `linked`, draft one page each,
following the page section of REPORT.md. The title is the error text itself, because that is
what people search for. For each cause, open the code path that raises the error, read the
conditions that lead to it, and cite `path:line`. A cause you could not trace goes under
"Not confirmed", not under causes. Each fix names what the user types or changes, and how
they can confirm it worked.

Build the slug from the anchor: lowercase it, replace every run of characters outside `a-z0-9`
with one hyphen, trim hyphens from both ends, and keep the first six words. An error code
becomes the slug on its own, lowercased. So `workflow.code` becomes `workflow-code`. The page
URL is `docs_base_url` + `/` + slug. If `docs_base_url` is empty, use
`https://<docs-site>/errors`, leave it visibly as a placeholder, and say so.

## 7. Link back

For each drafted page, write the smallest change that makes the error point to its page: the
original line, and the same line with the URL or code appended, as a unified diff with its
`path:line`. Next.js prints `https://nextjs.org/docs/messages/<slug>` in its errors and Rust
prints `rustc --explain E0XXX`. Following that pattern turns every future occurrence of the
error into a link to the page. Keep each change to one line and keep the message's existing
wording, so the page title still matches what users already search for. Do not apply the
change.

## 8. Write the report

Write `/home/user/state/reports/ERROR_PAGES.md` using REPORT.md. Set `status: complete` only if
every station ran within its limits; otherwise use `status: partial` and name the station that
stopped and why. A short, honest report beats a long one built on guesses.
