/* Members establish identity; saved author records keep guide and run bindings stable. */
(() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const label = author => `${author.display_name}${author.email && author.email !== author.display_name ? ` · ${author.email}` : ""}${author.is_me ? " (you)" : ""}`;
  async function members(services) {
    return services.api(`/api/projects/${services.projectId}/authors?members=true`);
  }
  async function selectMember(services, author) {
    return services.api(`/api/projects/${services.projectId}/authors/member/${encodeURIComponent(author.member_clerk_user_id)}`, {method: "POST"});
  }
  function field(name, value, title, id) {
    return `<div data-project-author-input data-selected-author="${esc(value)}">
      <select class="workflow-inline-input" name="${esc(name)}" id="${esc(id)}" aria-label="${esc(title)}" disabled><option value="${esc(value)}">Loading project members…</option></select>
      <p class="system-config-note" data-author-status role="status"></p>
      <span class="project-file-choice"><a class="system-quiet-action" data-author-guide target="_blank" rel="noopener" hidden>Open writing guide</a>
      <button type="button" class="system-quiet-action" data-author-choose hidden>Use an existing writing guide</button></span>
      <div data-author-guide-picker hidden>
        <select class="workflow-inline-input" aria-label="Existing writing guide" data-author-guide-file></select>
        <span class="project-file-choice"><button type="button" class="system-quiet-action" data-author-guide-save>Use this guide</button></span>
      </div>
    </div>`;
  }
  function bind(root, services) {
    const {projectId, api, isCurrent} = services;
    for (const el of root.querySelectorAll("[data-project-author-input]")) {
      if (el.dataset.bound) continue;
      el.dataset.bound = "true";
      const select = el.querySelector("select"), status = el.querySelector("[data-author-status]");
      const guide = el.querySelector("[data-author-guide]"), choose = el.querySelector("[data-author-choose]");
      const picker = el.querySelector("[data-author-guide-picker]"), file = el.querySelector("[data-author-guide-file]");
      const save = el.querySelector("[data-author-guide-save]");
      const current = () => isCurrent() && el.isConnected;
      let authors = [], generation = 0, selected, notifying = false;
      async function showGuide() {
        const request = ++generation;
        selected = null;
        const author = authors.find(a => a.id === select.value);
        guide.hidden = choose.hidden = picker.hidden = true;
        guide.removeAttribute("href");
        status.textContent = author ? "Checking writing guide…" : "Choose a project member.";
        if (!author) return;
        select.disabled = true;
        try {
          const bound = await selectMember(services, author);
          if (!current() || request !== generation) return;
          select.selectedOptions[0].value = bound.id;
          Object.assign(author, bound); selected = author;
          select.value = bound.id;
          const files = await TinProjectFileInput.list(root, services);
          if (!current() || request !== generation) return;
          const found = files.files.some(f => f.path === bound.guide_path);
          status.textContent = found ? `Uses ${author.display_name}'s saved writing guide.` : "Choose an existing guide, or use Learn my writing style to prepare one.";
          choose.hidden = false;
          choose.textContent = found ? "Change writing guide" : "Use an existing writing guide";
          if (found) {
            guide.href = `/file?${new URLSearchParams({project: projectId, path: bound.guide_path, revision: files.revision})}`;
            guide.hidden = false;
          }
          // Recheck setup only after the binding exists; the initial choice can race admission checks.
          notifying = true;
          try {select.dispatchEvent(new Event("change", {bubbles: true}));}
          finally {notifying = false;}
        } catch (error) {
          if (current() && request === generation) status.textContent = error.message;
        } finally {
          if (current() && request === generation) select.disabled = false;
        }
      }
      select.addEventListener("change", () => {if (!notifying) showGuide();});
      choose.onclick = async () => {
        const request = generation;
        try {
          const files = await TinProjectFileInput.list(root, services, true);
          if (!current() || request !== generation) return;
          file.replaceChildren(new Option("Choose a writing guide…", ""), ...files.files
            .filter(f => f.path.endsWith(".md") && f.path !== ".agents/skills/writing-style/SKILL.md")
            .map(f => new Option(f.path, f.path)));
          picker.hidden = false;
          file.focus();
        } catch (error) {if (current() && request === generation) status.textContent = error.message;}
      };
      save.onclick = async () => {
        const author = selected, request = generation;
        if (!author || !file.value) return;
        save.disabled = select.disabled = true;
        try {
          const updated = await api(`/api/projects/${projectId}/authors/${author.id}`, {method: "PUT", body: JSON.stringify({display_name: author.display_name, expected_version: author.version, selected_guide: file.value})});
          if (!current() || request !== generation) return;
          Object.assign(author, updated);
          select.dispatchEvent(new Event("change", {bubbles: true}));
        } catch (error) {if (current() && request === generation) status.textContent = error.message;}
        finally {if (current()) {save.disabled = false; if (request === generation) select.disabled = false;}}
      };
      members(services).then(response => {
        if (!current()) return;
        authors = response.authors;
        const value = el.dataset.selectedAuthor || response.default_author_id || "";
        select.replaceChildren(new Option("Choose a project member…", ""), ...authors.map(a => new Option(label(a), a.id)));
        if (value && !authors.some(a => a.id === value)) select.add(new Option("Unavailable author — choose a project member", value));
        select.value = value; select.disabled = false;
        select.dispatchEvent(new Event("change", {bubbles: true}));
      }).catch(() => {if (current()) status.textContent = "Could not load project members. Reopen setup to try again.";});
    }
  }
  window.TinProjectAuthorInput = {field, bind, members, selectMember, label};
})();
