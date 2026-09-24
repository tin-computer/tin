# Report format

Write `reports/ERROR_PAGES.md` with these sections in this order. Keep the headings exactly as
written so runs can be compared week to week.

```markdown
# Error pages

status: complete | partial
repository: <owner/name at commit sha, if known>
docs_base_url: <value or placeholder>

## Summary

Three to five sentences: how many user-visible errors exist, how many have no answer, how
many are already answered by someone else, and the one page to publish first and why.

## Funnel

| station | in | out | dropped because |
|---|---|---|---|
| harvest | – | N | – |
| reaches a person | N | N | log-only, … |
| stable anchor | N | N | too generic, … |
| not internal | N | N | assertion, … |
| after merge | N | N | duplicates |

## Error inventory

| # | anchor | surface | path:line | coverage | demand | reach | gap | self_fixable | score |
|---|---|---|---|---|---|---|---|---|---|

Every kept message appears once, highest score first. `demand` cites a URL when it is
`third_party` or `competitor`.

## Drafted pages

One subsection per drafted page:

### <exact error text>

- URL: <docs_base_url>/<slug>
- Fires at: `path:line` (surface)
- Score: reach × gap × self_fixable = N

**What it means.** One or two plain sentences.

**Why it happens.** Numbered causes, each with the `path:line` of the condition you read.

**How to fix it.** Numbered steps matching the causes, each with the exact command, setting or
value, and how to confirm it worked.

**Not confirmed.** Causes you suspect but could not trace in code. Omit if empty.

## Link-back changes

One unified diff per drafted page, unapplied, with its `path:line`.

## Not read

Directories, languages or surfaces you skipped, searches you did not run, and why.
```

Do not add a recommendations section beyond the pages and diffs. The report is the work, not
advice about the work.
