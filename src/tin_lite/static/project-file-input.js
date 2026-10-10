/* Ordinary project-file inputs. Values stay paths in the saved workflow schema. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function field(name, value, label, id, required = false) {
    return `<span data-project-file-input data-file-required="${required}">
      <span class="project-file-choice"><span data-file-label>${esc(value ? value.split("/").at(-1) : "No file selected")}</span>
      <a data-file-open class="system-quiet-action" target="_blank" rel="noopener" hidden>Open</a>
      <button type="button" class="system-quiet-action" data-file-change aria-expanded="false" aria-controls="${esc(id)}">Change</button></span>
      <select class="workflow-inline-input" id="${esc(id)}" name="${esc(name)}" aria-label="${esc(label)}" hidden><option value="${esc(value)}">${esc(value || "Choose a project file…")}</option></select>
      <small class="workflow-field-message" data-file-status role="status">Checking project Files…</small>
    </span>`;
  }
  function bind(root, {projectId, api, isCurrent}) {
    const fields = [...root.querySelectorAll("[data-project-file-input]")].filter(el => !el.dataset.bound);
    if (!fields.length || !projectId) return;
    let listing;
    const load = () => listing ||= api(`/api/projects/${encodeURIComponent(projectId)}/files`).catch(error => {listing = null; throw error;});
    for (const el of fields) {
      el.dataset.bound = "true";
      const select = el.querySelector("select"), label = el.querySelector("[data-file-label]");
      const open = el.querySelector("[data-file-open]"), change = el.querySelector("[data-file-change]");
      const status = el.querySelector("[data-file-status]");
      let snapshot;
      const current = () => isCurrent() && el.isConnected;
      const reveal = () => {select.hidden = false; change.setAttribute("aria-expanded", "true");};
      const render = () => {
        const value = select.value;
        const found = snapshot.files.some(file => file.path === value);
        label.textContent = value ? value.split("/").at(-1) : "No file selected";
        label.title = value;
        open.hidden = !found;
        open.removeAttribute("href");
        if (found) open.href = `/file?${new URLSearchParams({project: projectId, path: value, revision: snapshot.revision})}`;
        status.textContent = found ? "Found in project Files." : value ? el.dataset.fileRequired === "true" ? "Missing from project Files. Choose a file or add it with your coding agent." : "Not found in project Files." : "No file selected.";
        if (!found && el.dataset.fileRequired === "true") {
          reveal();
          const details = el.closest("details.x-workflow-details");
          if (details) details.open = true;
        }
      };
      async function refresh() {
        try {
          const data = await load();
          if (!current()) return;
          snapshot = data;
          const value = select.value;
          const paths = [...new Set([value, ...data.files.map(file => file.path)])].filter(Boolean);
          select.replaceChildren(new Option("Choose a project file…", ""), ...paths.map(path => new Option(path, path)));
          select.value = value;
          render();
        } catch {
          if (!current()) return;
          open.hidden = true;
          status.textContent = "Could not check project Files. Select Change to retry.";
        }
      }
      change.onclick = async () => {
        if (!current()) return;
        reveal();
        listing = null;
        await refresh();
        if (current()) select.focus();
      };
      select.addEventListener("change", () => {if (current() && snapshot) render();});
      refresh();
    }
  }
  window.TinProjectFileInput = {field, bind};
})();
