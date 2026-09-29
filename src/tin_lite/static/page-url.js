/* A draft meant to become a website page gets one plain line.
   Card, before approval: where the page would appear. Nothing when Tin found no page on the
   site that shows the file's folder.
   Document, after approval: the pull request, the deploy, then the live address as a link. */
(() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const safeUrl = (value) => typeof value === "string" && /^https:\/\/[^\s"'<>`]+$/.test(value);
  const asked = new Set();

  function site(url) {
    try {
      return new URL(url).host;
    } catch {
      return "your site";
    }
  }

  function line(mode, runId, page, text) {
    const due = page?.checkable ? " data-page-url-due" : "";
    const kind = mode === "document" ? "page-url-status" : "page-url-line";
    return `<p class="${kind}" data-page-url="${esc(runId)}" data-page-url-mode="${mode}"${due}>${text}</p>`;
  }

  // A saved address is missing: ask once, then draw the line in place.
  function placeholder(mode, runId) {
    return runId ? `<p class="page-url-${mode === "document" ? "status" : "line"}" data-page-url="${esc(runId)}" data-page-url-mode="${mode}" data-page-url-due hidden></p>` : "";
  }

  function card(page, runId, {pending = false} = {}) {
    if (!page || !safeUrl(page.url)) return pending ? placeholder("card", runId) : "";
    if (page.route_missing || page.pull_request || !["proposed", "planned"].includes(page.state)) return "";
    const label = page.state === "planned" ? "Will be published at" : "Proposed URL";
    return line("card", runId, page, `<span class="page-url-label">${label}</span> <code>${esc(page.url)}</code>`);
  }

  function documentLine(page, runId, {pending = false} = {}) {
    if (!page || !safeUrl(page.url)) return pending ? placeholder("document", runId) : "";
    const host = esc(site(page.url));
    if (page.state === "live") {
      return line("document", runId, page, `Live at <a href="${esc(page.url)}" target="_blank" rel="noopener noreferrer">${esc(page.url)} ↗</a>`);
    }
    const merged = page.pull_request ? "Merged" : "Committed";
    if (page.state === "merged") {
      return line("document", runId, page, page.deploy_overdue
        ? `${merged}, but not a page on ${host} yet.`
        : `${merged}. Waiting for ${host} to deploy it.`);
    }
    if (page.pull_request) {
      const number = Number.isInteger(page.pull_request.number) ? ` #${page.pull_request.number}` : "";
      return line("document", runId, page, `Pull request${number} is open; the page goes live after you merge it.`);
    }
    return "";
  }

  const render = {card, document: documentLine};

  // Ask the server once per page load for each due line; it rate-limits real checks itself.
  function bind(root, {api, onUpdate} = {}) {
    if (!root || typeof api !== "function") return;
    root.querySelectorAll("[data-page-url][data-page-url-due]").forEach((element) => {
      const id = element.dataset.pageUrl;
      const mode = element.dataset.pageUrlMode === "document" ? "document" : "card";
      if (!id || asked.has(`${mode}:${id}`)) return;
      asked.add(`${mode}:${id}`);
      api(`/api/workflows/runs/${encodeURIComponent(id)}/page-url?check=true`)
        .then((result) => {
          const page = result?.page_url;
          if (!page) return;
          onUpdate?.(id, page);
          if (element.isConnected) element.outerHTML = render[mode](page, id);
        })
        .catch(() => {});
    });
  }

  window.TinPageUrl = {card, document: documentLine, bind};
})();
