# Deliver the approved page

Run the article-delivery skill. This is adaptation, not writing or rewriting.
The trusted `workspace.content_delivery` context supplies the approved copy (`article`, in
Markdown), its source identity and the destination repository. `source_kind` says what it
is: `article` (a planned article, with its plan `item`), `answer_page` or `public_article`.
Answer pages and public articles carry their search listing apart from the copy in
`page_metadata` (`meta_title`, `meta_description`, and the approved `slug` when the draft
chose one). Keep the Markdown in Tin unchanged.
Inspect this repository's instructions and existing pages, and prepare one coherent site
change (at most 30 files) as an unmerged PR.

Build the page the way this site builds its pages: a Markdown or MDX file, a component, a
typed page registry, plain HTML or whatever else the repository uses. Convert the format
freely and wrap it in the site's own layout, styles and components. Keep the approved
wording: the same headings, paragraphs, lists, tables, caveats and link destinations, in
the same order. Do not summarize it, rewrite it or add sales copy. Put the listing in the
site's own title and description fields, never into the copy. Prefer the site's existing
tools; add a dependency only when the page cannot be built without one, and say why in the
PR body.

Match the site's other pages of that kind: give the page the same header, footer, links
back into the site (for example to the product), byline and dates, and structured data (such as
JSON-LD) that they carry. Use only the fonts and files the site already loads; do not add web
fonts or other outside resources.

The approved page may carry figures and interactive pieces, listed in `assets` with each
file's project path, and diagram, video and callout blocks in `article`. Your project-state
checkout is at the approved revision: copy each asset from `/home/user/state/<path>` into the
place this site keeps such files, and show it the way the site shows media. An SVG becomes an
image or the site's figure component, with its alt text and caption. An interactive piece
becomes an iframe or an inline component, with its script and data unchanged. Show each
`mermaid` block with the site's own Mermaid support, or draw it as an SVG figure in the site's
style when the site has none. Show each `tin-video` block with the site's video component or the provider's embed
code, linking a video file rather than copying it, and each `> [!NOTE]`-style callout with
the site's own note style, or a plain aside. Every approved asset, diagram and video has to
appear on the page.

If the site does not serve the page's route yet, add the smallest route that fits how its
other pages are served. Change what the page needs (the page, its route, and any index or
sitemap the site keeps) and nothing else. Never move, rename or remove another page.

Run available relevant checks. Never claim a full build succeeded if dependencies,
network access, configuration or services prevent it. The mandatory Tin check is
`git diff --check`; it is not a site build or factual check. In the PR body separate
checks actually run and their results from checks not run, with reasons. Include the
source page's Tin run ID and a request to inspect the site preview before merging.
Put the page's address on its own line, `Public URL: https://<site host>/<route>`: the
full URL the page will have once this PR merges and the site deploys, worked out from
the route you added. Write `Public URL: unknown` if the repository does not show it.
No merge, deployment, publication or outreach.
