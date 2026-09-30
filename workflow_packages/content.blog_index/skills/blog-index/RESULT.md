# Pull request body and receipt

Use these headings, in this order, for the pull request body and the receipt at
`reports/blog-index/{run_id}/RESULT.md`. A no-change result uses the same headings, with
`Outcome` naming the reason and the change sections saying none.

## Outcome

`patch` or `no_change`, the mode, the repository and its revision, and one sentence: how many
eligible posts are now within two clicks of the index at which route. For no-change, the
reason: `check_only`, `nothing_to_fix`, `no_article_route`, `source_not_found` or
`hosted_blog`.

## Where articles live

The route used and where it came from (`content/page-routes.json`, or the repository's post
route), the post source with its `path:line`, and its exclusion rules.

## Numbers

From MEASUREMENT.md.

## What changed

A table of changes a-g from the skill: before, after, the file and status (done, deferred or
not needed). Then how a new post appears: it reaches the index, sitemap and feed on the next
build with no template edit.

## Verification

Checks run with their results, then checks not run and why, and up to ten URLs to inspect by
hand after deploy.

## Not changed

Posts, post URLs, feeds' GUIDs, build and deploy settings, and anything an open pull request
already changes.

For the reason `no_article_route`, end the receipt with this block, which the coding agent
reads to ask the founder once:

````markdown
```json ask-the-founder
{"question": "Where on your site should articles go?", "suggestion": "/blog/{slug}",
 "how_to_suggest": "Look at the site's existing routes first and suggest the folder its related content already uses, such as /blog, /guides, /learn or /resources; if it has none, suggest /blog/{slug}. Use a word the site's readers would use; never a Tin term. Say it in one line with a full example address, and ask the founder to confirm or name another.",
 "then": {"name": "save_page_route", "arguments": {"page_type": "article", "route": "<the route the founder confirmed>"}}}
```
````

Replace the suggestion with the folder the repository already uses for related content when
there is one. Every receipt ends with:

````markdown
```json blog-index
{"version": 1, "outcome": "patch", "route": "/blog/{slug}", "posts": 34, "within_two_clicks": 34}
```
````
