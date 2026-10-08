# The plan

Write one Markdown file, the plan at `reports/feedback-to-fix/{run_id}/PLAN.md`
(context.output.path). Tin checks its shape before it keeps it. For `patch`, Tin records the
change in Decisions; once the founder approves it there, website.change writes the files and
opens the pull request. Use these headings, in this order. A result with no change uses the
same headings, with `Outcome` naming the reason and the change sections saying none.

## Outcome

`patch`, `report_only` or `insufficient_data`, the repository and the commit you read, and in
one sentence what a visitor would read differently. For anything but `patch`, THEMES.md's
reason and the smallest step that would change the result.

## Themes

A table of every theme with at least two different people, in the order `choose_action`
ranked them: `id`, kind, number of different people, number of quotes, and what was decided for
it (`planned`, or the reason it was not). Then `Seen once`: every one-person observation as its
verbatim quote with a plain Markdown link and the theme it would join, labelled not a theme and
not acted on. A founder should see what is one voice short of the threshold.

## Evidence

For each theme, every quote verbatim with its link, written as a plain Markdown link. No
usernames, no citation markers or other tool tokens after a quote. Say how many people and how
many threads, and the date range read.

## The change

For `patch`: each changed file with the before and after text of each edit, and the quote ids
that caused it. Copy that answers a quote in the same words is not a fix; say what the wording
now makes clear.

## Not patched

Themes the decision set aside with the reason, including every `product_change` theme written
as one sentence a person can turn into a task.

## Sources read

Each platform searched and the threads opened, with a count. Name every platform or page that
could not be opened or searched and what that means for the result, and say when the thread
limit stopped the search.

## Verification

The checks you ran on the scratch copy and their results, then the checks not run and why.
Always include a manual step: after the change is on the site, read the page as a first-time
visitor and confirm the point in each quote is now answered.

## Not changed

Pricing, legal text, code, new pages, and anything a quote asked for that the product does not
do.

## Patch

Only for `patch`: the files website.change writes, between these markers, as one JSON object in
a fenced block. It is the `feedback-fix-patch/1` contract, shared with website.change; use
exactly these keys. Leave the section and the markers out of any other outcome.

````markdown
<!-- feedback-fix-patch.json:start -->
```json
{"schema": "feedback-fix-patch/1", "repository": "owner/repo", "base_ref": "main",
 "base_sha": "<workspace.head_sha, the commit you read>", "route": "/",
 "summary": "One sentence on what a visitor now reads differently.",
 "kind": "unclear_what_it_is", "people": 2,
 "quotes": ["<the link of every quote behind the planned theme>"],
 "edits": [{"path": "src/app/page.tsx", "before": "<the old wording>",
            "after": "<the new wording>"}],
 "files": [{"path": "src/app/page.tsx", "action": "update",
            "content": "<the file's full text after the change>"}],
 "caps": {"max_files": 3}}
```
<!-- feedback-fix-patch.json:end -->
````

- `repository`, `base_ref` and `base_sha` come from the run context's `workspace`
  (`repository`, `default_branch`, `head_sha`).
- `route` is the site path of the page whose copy changes, such as `/` or `/pricing`.
- `kind` and `people` are the planned theme's, as `choose_action` counted them; `quotes` holds
  the links of its quotes.
- `edits` holds each changed passage, at most six, each `before` and `after` at most 500
  characters: the founder approves these in Decisions, so they must be the exact old and new
  wording. Every `after` appears in its file's `content`.
- `files` holds only the files `choose_action` returned, each `update` with its full text after
  the change. Build the block with code from the edited scratch copy (read each file, then
  `json.dumps`), never by retyping a file.

End the plan with a fenced block whose info string is `json feedback-to-fix`:
`{"outcome": "patch", "reason": ""}`, with `report_only` or `insufficient_data` and THEMES.md's
reason for a plan without a patch.
