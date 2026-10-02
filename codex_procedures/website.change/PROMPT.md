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
the exact approved copy (`article`), its source identity, the destination repository,
the page's route (`route`), the protected paths (`protected_paths`) and whether Tin may
publish it (`publish`). Today every change is an approved page (`change.source` is
`content_draft`): `source_kind` says what it is, `article` (a planned article, with its
plan `item`), `answer_page` or `public_article`. Answer pages and public articles carry
their search listing apart from the copy in `page_metadata` (`meta_title`,
`meta_description`). Keep the Markdown in Tin unchanged. Inspect this repository's
instructions and existing page routes, components and typed page registries. Prepare the
smallest coherent site change (at most five text files) and a PR.

Keep `article` byte-for-byte in one Markdown file, or as a complete JSON string literal
consumed by an existing Markdown renderer. Do not transcribe its prose into JSX,
summarize it, remove caveats or alter links. Put the listing in the site's own title and
description fields, never into the copy. Do not add dependencies, and never touch
`package.json` or a lockfile.

When `route` is set, the founder chose where these pages live: publish the page at exactly
that route, in the site's own page registry or content folder for such pages. Never put a
page under `content/answers/`, which is Tin's draft folder and not a page on the site.
Never change a file that serves a path in `protected_paths` (such as /sign-in), a root
layout, middleware, or host or build settings.

When the site already renders Markdown files from a content folder, add the page there as
one `.md` file. When no route renders such a folder but the site already depends on a
Markdown renderer, you may add one minimal route, once, that renders `.md` files from one
content folder with that renderer and the site's existing layout, and place the page there
as a `.md` file. If the site has no Markdown renderer, stop and explain the missing
prerequisite; never commit a file the site will not render.

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
