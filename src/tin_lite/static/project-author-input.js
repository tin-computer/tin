/* Confirmed project authors; display names never establish identity. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function field(name, value, label, id) {
    return `<div data-project-author-input data-selected-author="${esc(value)}">
      <select class="workflow-inline-input" name="${esc(name)}" id="${esc(id)}" aria-label="${esc(label)}" disabled><option value="${esc(value)}">Loading authors…</option></select>
      <p class="system-config-note" data-author-status role="status"></p>
      <a class="system-quiet-action" data-author-guide target="_blank" rel="noopener" hidden>Open writing guide</a>
      <details class="x-workflow-details"><summary>Add an author</summary><div>
        <label class="system-setting">Name<input class="workflow-inline-input" data-author-name maxlength="200"></label>
        <label><input type="checkbox" data-author-me> This is me</label>
        <p class="system-config-note">Use Learn my writing style to prepare this author's guide.</p>
        <button type="button" class="system-quiet-action" data-author-add>Add author</button>
      </div></details>
    </div>`;
  }
  function bind(root, services) {
    const {projectId, api, isCurrent} = services;
    for (const el of root.querySelectorAll("[data-project-author-input]")) {
      if (el.dataset.bound) continue;
      el.dataset.bound = "true";
      const select = el.querySelector("select"), status = el.querySelector("[data-author-status]");
      const guide = el.querySelector("[data-author-guide]"), add = el.querySelector("[data-author-add]");
      const current = () => isCurrent() && el.isConnected;
      let authors = [], generation = 0;
      async function showGuide() {
        const request = ++generation;
        const author = authors.find(a => a.id === select.value);
        guide.hidden = true; guide.removeAttribute("href");
        status.textContent = author ? "Checking writing guide…" : "Choose an author, or add one below.";
        if (!author) return;
        try {
          const files = await TinProjectFileInput.list(root, services);
          if (!current() || request !== generation) return;
          const found = files.files.some(file => file.path === author.guide_path);
          status.textContent = found ? `Uses ${author.display_name}'s saved writing guide.` : "Writing guide missing. Use Learn my writing style to prepare it.";
          if (found) {
            guide.href = `/file?${new URLSearchParams({project: projectId, path: author.guide_path, revision: files.revision})}`;
            guide.hidden = false;
          }
        } catch {
          if (current() && request === generation) status.textContent = "Could not check the writing guide. Reopen setup to try again.";
        }
      }
      async function load(chosen) {
        const response = await api(`/api/projects/${projectId}/authors`);
        if (!current()) return;
        authors = response.authors;
        const value = chosen || response.default_author_id || "";
        select.replaceChildren(new Option("Choose an author…", ""), ...authors.map(a => {
          const duplicate = authors.filter(other => other.display_name === a.display_name).length > 1;
          return new Option(`${a.display_name}${duplicate ? ` · ${a.id.slice(0, 8)}` : ""}${a.id === response.default_author_id ? " (you)" : ""}`, a.id);
        }));
        if (value && !authors.some(a => a.id === value)) select.add(new Option("Unavailable author — choose another", value));
        select.value = value; select.disabled = false;
        select.dispatchEvent(new Event("change", {bubbles: true}));
      }
      select.addEventListener("change", showGuide);
      add.onclick = async () => {
        const name = el.querySelector("[data-author-name]").value.trim();
        if (!name) {status.textContent = "Enter the author's name."; return;}
        add.disabled = true;
        try {
          const id = crypto.randomUUID();
          await api(`/api/projects/${projectId}/authors/${id}`, {method: "PUT", body: JSON.stringify({display_name: name, expected_version: 0, link_to_me: el.querySelector("[data-author-me]").checked})});
          if (!current()) return;
          await load(id);
          el.querySelector("details").open = false;
        } catch (error) {if (current()) status.textContent = error.message;}
        finally {if (current()) add.disabled = false;}
      };
      load(el.dataset.selectedAuthor).catch(() => {if (current()) status.textContent = "Could not load authors. Reopen setup to try again.";});
    }
  }
  window.TinProjectAuthorInput = {field, bind};
})();
