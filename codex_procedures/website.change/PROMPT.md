# Make approved changes to the website

This workflow edits the founder's website. Read `workspace.website_change.source` in the
trusted context first:

- An approved page (no `source`, or `content_draft`): run the site-change skill. It makes
  exactly one change, the change row in `workspace.website_change`. This is adaptation, not
  writing or rewriting. The rest of this prompt is about that case.
- `audit` or `planned`: run the site-repair skill instead. It makes the changes in
  `workspace.technical_fix` (site-fix-v5's plan: the audit's findings, or the redirects and
  noindex changes page decisions and the site plan made) in one bounded pull request. Return
  outcome `patch` with reason `""`, or outcome `no_change` with reason `no_safe_patch` and
  no files. Never write marketing copy, and never touch a path in
  `workspace.website_change.protected_paths`.

The context holds the change row (`change`: its source, stable ID, kind and site paths),
the approved copy (`article`, in Markdown), its source identity, the destination repository,
the page's route (`route`), the protected paths (`protected_paths`) and whether Tin may
publish it (`publish`). Today every change is an approved page (`change.source` is
`content_draft`): `source_kind` says what it is, `article` (a planned article, with its
plan `item`), `answer_page` or `public_article`. Answer pages and public articles carry
their search listing apart from the copy in `page_metadata` (`meta_title`,
`meta_description`). Keep the Markdown in Tin unchanged. Inspect this repository's
instructions and existing pages, routes, components and page registries, and prepare one
coherent site change (at most 20 files) as a PR.

Build the page the way this site builds its pages: a Markdown or MDX file, a component, a
typed page registry, plain HTML or whatever else the repository uses. Convert the format
freely and wrap it in the site's own layout, styles and components. Keep the approved
wording: the same headings, paragraphs, lists, tables, caveats and link destinations, in
the same order. Do not summarize it, rewrite it or add sales copy. Put the listing in the
site's own title and description fields, never into the copy. Prefer the site's existing
tools; add a dependency only when the page cannot be built without one, and say why in the
PR body.

When `route` is set, the founder chose where these pages live: publish the page at exactly
that route, wherever the site keeps such pages. Never put a
page under `content/answers/`, which is Tin's draft folder and not a page on the site.
Never change a file that serves a path in `protected_paths` (such as /sign-in). Leave
root layouts, middleware and host or build settings alone unless the page cannot be served
without a change there, and then say why in the PR body.

If the site does not serve the page's route yet, add the smallest route that fits how its
other pages are served. The page must actually be served, not left in a folder nothing
reads.

Run available relevant checks. Never claim a full build succeeded if dependencies,
network access, configuration or services prevent it. The mandatory Tin check is
`git diff --check`; it is not a site build or factual check. In the PR body separate
checks actually run and their results from checks not run, with reasons. Include the
change ID, the source page's Tin run ID and a request to inspect the site preview. Put
the page's address on its own line, `Public URL: https://<site host>/<route>`: the full
URL the page will have once this PR merges and the site deploys. Write
`Public URL: unknown` if the repository does not show it. You do not merge, deploy or
send outreach: Tin merges the PR only when `publish.mode` is `direct` and the repository's
required checks pass, and otherwise the founder merges it.
