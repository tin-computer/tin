# The plan

Write one Markdown file, `reports/blog-index/{run_id}/PLAN.md` (context.output.path). Tin
checks its shape before it keeps it, and website.change (`source: blog_index`) applies the
patch only after the founder approves it. Use these headings, in this order. A no-change plan
uses the same headings, with `Outcome` naming the reason and the change sections saying none.

## Outcome

`plan` or `no_change`, the mode, the repository and the commit you read, and one sentence:
how many eligible posts the planned index puts within two clicks, at which route. For
no-change, the reason: `check_only`, `nothing_to_fix`, `no_article_route`, `source_not_found`
or `hosted_blog`.

## Where articles live

The route used and where it came from (`content/page-routes.json`, or the repository's post
route), the post source with its `path:line`, and its exclusion rules.

## Numbers

From MEASUREMENT.md.

## What changes

A table of changes a-g from the skill: before, after, the file and status (planned, deferred
or not needed). Then how a new post appears once website.change has applied the plan: it
reaches the index, sitemap and feed on the next build with no template edit.

## Verification

Checks run in the scratch copy with their results, then checks not run and why, and up to ten
URLs to inspect by hand after deploy.

## Not changed

Posts, post URLs, feeds' GUIDs, package.json and lockfiles, build, CI and deploy settings.

## Patch

The files website.change writes, between these markers, as one JSON object in a fenced block.
It is the `blog-index-patch/1` contract, shared with website.change; use exactly these keys:

````markdown
<!-- blog-index-patch.json:start -->
```json
{"schema": "blog-index-patch/1", "repository": "owner/repo", "base_ref": "main",
 "base_sha": "<workspace.head_sha, the commit you read>", "route": "/blog",
 "summary": "One sentence on what the files do.",
 "files": [{"path": "src/pages/blog/index.astro", "action": "create",
            "content": "<the file's full text after the change>"}],
 "caps": {"max_files": 5}}
```
<!-- blog-index-patch.json:end -->
````

- `repository`, `base_ref` and `base_sha` come from the run context's `workspace`
  (`repository`, `default_branch`, `head_sha`).
- `route` is the index's route, the folder of the article route (`/blog` for
  `/blog/{slug}`). It is empty only for a no-change plan without a route for articles.
- `files` holds at most five files, each with its repository-relative `path`, `action`
  (`create` or `update`) and its full `content`, at most 120,000 bytes together. Never
  package.json, a lockfile, or build, CI or deploy settings. A no-change plan has `"files": []`.
- `caps` is always `{"max_files": 5}`.

For the reason `no_article_route`, add this block after the patch, which the coding agent
reads to ask the founder once:

````markdown
```json ask-the-founder
{"question": "Where on your site should articles go?", "suggestion": "/blog/{slug}",
 "how_to_suggest": "Look at the site's existing routes first and suggest the folder its related content already uses, such as /blog, /guides, /learn or /resources; if it has none, suggest /blog/{slug}. Use a word the site's readers would use; never a Tin term. Say it in one line with a full example address, and ask the founder to confirm or name another.",
 "then": {"name": "save_page_route", "arguments": {"page_type": "article", "route": "<the route the founder confirmed>"}}}
```
````

Replace the suggestion with the folder the repository already uses for related content when
there is one. Every plan ends with this block; `reason` is null for a plan with files:

````markdown
```json blog-index
{"version": 1, "outcome": "plan", "reason": null, "route": "/blog/{slug}", "posts": 34, "within_two_clicks": 34}
```
````
