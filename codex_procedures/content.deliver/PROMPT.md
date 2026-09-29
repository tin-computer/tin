# Deliver the approved page

Run the article-delivery skill. This is adaptation, not writing or rewriting.
The trusted `workspace.content_delivery` context supplies the exact approved copy
(`article`), its source identity and the destination repository. `source_kind` says what
it is: `article` (a planned article, with its plan `item`), `answer_page` or
`public_article`. Answer pages and public articles carry their search listing apart from
the copy in `page_metadata` (`meta_title`, `meta_description`). Keep the Markdown in Tin
unchanged. Inspect this repository's instructions and existing page routes/components.
Prepare the smallest coherent site change (at most five text files) and an unmerged PR.

Keep `article` byte-for-byte in one Markdown file, or as a complete JSON string literal
consumed by an existing Markdown renderer. Do not transcribe its prose into JSX,
summarize it, remove caveats or alter links. Put the listing in the site's own title and
description fields, never into the copy. Do not add dependencies just to render it.

When the site already renders Markdown files from a content folder, add the page there as
one `.md` file. When no route renders such a folder (for example, a Next.js site whose
pages are TypeScript objects) but the site already depends on a Markdown renderer, you may
add one minimal route, once, that renders `.md` files from one content folder with that
renderer and the site's existing layout, and place the page there as a `.md` file. If the
site has no Markdown renderer, stop and explain the missing prerequisite; never commit a
file the site will not render, and never pretend an unused data file is a delivered page.
Adapt only the wrapper, route, frontmatter, and required index.

Run available relevant checks. Never claim a full build succeeded if dependencies,
network access, configuration or services prevent it. The mandatory Tin check is
`git diff --check`; it is not a site build or factual check. In the PR body separate
checks actually run and their results from checks not run, with reasons. Include the
source page's Tin run ID and a request to inspect the site preview before merging.
Put the page's address on its own line, `Public URL: https://<site host>/<route>`: the
full URL the page will have once this PR merges and the site deploys, worked out from
the route you added. Write `Public URL: unknown` if the repository does not show it.
No merge, deployment, publication or outreach.
