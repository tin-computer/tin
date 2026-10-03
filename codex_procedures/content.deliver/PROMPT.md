# Deliver the approved page

Run the article-delivery skill. This is adaptation, not writing or rewriting.
The trusted `workspace.content_delivery` context supplies the approved copy (`article`, in
Markdown), its source identity and the destination repository. `source_kind` says what it
is: `article` (a planned article, with its plan `item`), `answer_page` or `public_article`.
Answer pages and public articles carry their search listing apart from the copy in
`page_metadata` (`meta_title`, `meta_description`). Keep the Markdown in Tin unchanged.
Inspect this repository's instructions and existing pages, and prepare one coherent site
change (at most 10 files) as an unmerged PR.

Build the page the way this site builds its pages: a Markdown or MDX file, a component, a
typed page registry, plain HTML or whatever else the repository uses. Convert the format
freely and wrap it in the site's own layout, styles and components. Keep the approved
wording: the same headings, paragraphs, lists, tables, caveats and link destinations, in
the same order. Do not summarize it, rewrite it or add sales copy. Put the listing in the
site's own title and description fields, never into the copy. Prefer the site's existing
tools; add a dependency only when the page cannot be built without one, and say why in the
PR body.

If the site does not serve the page's route yet, add the smallest route that fits how its
other pages are served. Change what the page needs (the page, its route, and any index or
sitemap the site keeps) and nothing else.

Run available relevant checks. Never claim a full build succeeded if dependencies,
network access, configuration or services prevent it. The mandatory Tin check is
`git diff --check`; it is not a site build or factual check. In the PR body separate
checks actually run and their results from checks not run, with reasons. Include the
source page's Tin run ID and a request to inspect the site preview before merging.
Put the page's address on its own line, `Public URL: https://<site host>/<route>`: the
full URL the page will have once this PR merges and the site deploys, worked out from
the route you added. Write `Public URL: unknown` if the repository does not show it.
No merge, deployment, publication or outreach.
