/* One "Page URL" line for a draft meant to become a website page: where it would appear,
   and whether Tin has found it live. Only a live page is a link; a proposed URL may not exist. */
(() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const safeUrl = (value) => typeof value === "string" && /^https:\/\/[^\s"'<>`]+$/.test(value);
  const asked = new Set();

  function ago(value) {
    const minutes = Math.max(0, Math.round((Date.now() - Date.parse(value)) / 60000));
    if (!Number.isFinite(minutes)) return "";
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
  }

  function html(page, runId, {pending = false} = {}) {
    const id = esc(runId || "");
    if (!page || !safeUrl(page.url)) {
      // No saved address yet: ask once, then draw the line in place.
      return pending && runId ? `<p class="page-url-line" data-page-url="${id}" data-page-url-due hidden></p>` : "";
    }
    const address = page.state === "live"
      ? `<a href="${esc(page.url)}" target="_blank" rel="noopener noreferrer">${esc(page.url)} ↗</a>`
      : `<code>${esc(page.url)}</code>`;
    const checked = page.state === "live" && page.checked_at ? ` Checked ${esc(ago(page.checked_at))}.` : "";
    return `<p class="page-url-line is-${esc(page.state)}" data-page-url="${id}"${page.checkable ? " data-page-url-due" : ""}>
      <strong>${esc(page.label)}</strong> ${address}
      <span>${esc(page.note)}${checked}</span>
    </p>`;
  }

  // Ask the server once per page load for each due line; it rate-limits real checks itself.
  function bind(root, {api, onUpdate} = {}) {
    if (!root || typeof api !== "function") return;
    root.querySelectorAll("[data-page-url][data-page-url-due]").forEach((line) => {
      const id = line.dataset.pageUrl;
      if (!id || asked.has(id)) return;
      asked.add(id);
      api(`/api/workflows/runs/${encodeURIComponent(id)}/page-url?check=true`)
        .then((result) => {
          const page = result?.page_url;
          if (!page) return;
          onUpdate?.(id, page);
          if (line.isConnected) line.outerHTML = html(page, id);
        })
        .catch(() => {});
    });
  }

  window.TinPageUrl = {html, bind};
})();
