# Pull request body and receipt

Use these headings, in this order, for both the pull request body and the receipt at
`reports/feedback-to-fix/{run_id}/RESULT.md`. A result with no change uses the same headings,
with `Outcome` naming the reason and the change sections saying none.

## Outcome

`patch`, `report_only` or `insufficient_data`, the repository and its revision, and in one
sentence what a visitor now reads differently. For anything but `patch`, THEMES.md's reason and
the smallest step that would change the result.

## Themes

A table of every theme with at least two different people, in the order `choose_action`
ranked them: `id`, kind, number of different people, number of quotes, and what was decided for
it (`patched`, or the reason it was not). Then `Seen once`: every one-person observation as its
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
could not be opened or searched and what that means for the result.

## Verification

The checks actually run and their results, then the checks not run and why. Always include a
manual step: read the changed page as a first-time visitor and confirm the point in each quote
is now answered on the page.

## Not changed

Pricing, legal text, code, new pages, and anything a quote asked for that the product does not
do.

End with a fenced block whose info string is `tin-feedback-to-fix`, holding JSON:
`{"version": 1, "quotes": ["<link of every quote whose theme this run patched>"]}`. The list is
empty unless the outcome is `patch`: a quote nothing was done about is counted again next run.
