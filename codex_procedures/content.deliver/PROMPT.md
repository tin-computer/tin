# Deliver the approved article

Run the article-delivery skill. This is adaptation, not writing or rewriting.
The trusted `workspace.content_delivery` context supplies the exact approved article,
its source identity and the destination repository. Keep that Markdown in Tin unchanged.
Inspect this repository's instructions and existing article routes/components. Prepare
the smallest coherent site change (at most five text files) and an unmerged PR.

Keep the approved article byte-for-byte in one Markdown file, or as a complete JSON
string literal consumed by an existing Markdown renderer. Do not transcribe its prose
into JSX, summarize it, remove caveats or alter links. Do not add dependencies just to
render it. If the repository cannot consume the article losslessly with its existing
tools, stop and explain the missing prerequisite; never pretend an unused data file
is a delivered article. Adapt only the wrapper, route, frontmatter, and required index.

Run available relevant checks. Never claim a full build succeeded if dependencies,
network access, configuration or services prevent it. The mandatory Tin check is
`git diff --check`; it is not a site build or factual check. In the PR body separate
checks actually run and their results from checks not run, with reasons. Include the
source article's Tin run ID and a request to inspect the site preview before merging.
Put the page's address on its own line, `Public URL: https://<site host>/<route>`: the
full URL the page will have once this PR merges and the site deploys, worked out from
the route you added. Write `Public URL: unknown` if the repository does not show it.
No merge, deployment, publication or outreach.
