/* Sample preparation only. The ordinary run endpoint performs extraction. */
(() => {
  const drafts = new Map();
  const authors = new Map();
  const bytes = (s) => new TextEncoder().encode(s).length;
  const escape = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function draft(projectId) {
    if (!drafts.has(projectId)) drafts.set(projectId, {samples: [], purpose: "Clear public articles", preferences: "", pending: null});
    return drafts.get(projectId);
  }
  function fields({authors = false} = {}) {
    return `<div data-style-capture>
      ${authors ? `<div class="x-workflow-fields" data-style-authors>
        <label class="system-setting"><strong>Capture for</strong><select class="workflow-inline-input" name="input:author_id" data-style-author disabled><option value="">Loading authors…</option></select></label>
        <p class="system-config-note" data-style-author-note></p>
      </div>` : ""}
      <p class="system-config-note">Your agent can help choose passages from notes, articles or conversations.</p>
      <button class="system-quiet-action" type="button" data-style-agent>Use your coding agent · copy prompt</button>
      <button class="button-quiet" type="button" data-style-connect>Connection instructions →</button>
      <details data-style-samples><summary class="system-quiet-action">Add samples here</summary>
        <label class="system-setting"><strong>What will you write?</strong><input class="workflow-inline-input" data-style-purpose maxlength="500"></label>
        <label class="system-setting"><strong>Preferences</strong><textarea class="workflow-inline-input workflow-inline-textarea" data-style-preferences maxlength="4000" placeholder="Anything to keep or avoid"></textarea></label>
        <label class="system-setting"><strong>Sample type</strong><select class="workflow-inline-input" data-style-kind>
          <option value="authored">My authored writing</option><option value="note">My notes</option>
          <option value="conversation">My conversation messages</option><option value="correction">My editing corrections</option>
          <option value="reference">A reference I want to learn from</option></select></label>
        <label class="system-setting"><strong>Paste a passage</strong><textarea class="workflow-inline-input workflow-inline-textarea" data-style-text maxlength="90000"></textarea></label>
        <button class="button-secondary" type="button" data-style-add>Add passage</button>
        <label class="system-setting"><strong>Or upload samples</strong><input type="file" data-style-upload accept=".md,.txt,.docx" multiple></label>
        <button class="button-quiet" type="button" data-style-files>Choose from project Files →</button>
        <select class="workflow-inline-input" data-style-project-file aria-label="Project writing sample" hidden></select>
        <button class="button-secondary" type="button" data-style-add-file hidden>Add selected file</button>
        <div data-style-list></div>
        <p class="system-config-note">Up to 8 samples. Selected text is saved in this project and visible to its members. Uploaded originals are converted, not retained. Capture saves an editable guide for future drafts.</p>
      </details>
      <p class="system-config-note" data-style-message role="status"></p>
      <input type="hidden" name="input:source_path">
      <input type="hidden" name="input:direction" value="">
    </div>`;
  }
  function bind(form, services) {
    const root = form.querySelector("[data-style-capture]");
    if (!root || root.dataset.bound) return;
    root.dataset.bound = "true";
    let data = draft(services.projectId);
    const el = (name) => root.querySelector(`[data-style-${name}]`);
    const message = (s) => { el("message").textContent = s; };
    const safe = (fn) => async () => { try { services.assertCurrent(); await fn(); } catch (e) { message(e.message); } };
    el("purpose").value = data.purpose;
    el("preferences").value = data.preferences;
    el("purpose").oninput = () => { data.purpose = el("purpose").value; data.pending = null; };
    el("preferences").oninput = () => { data.preferences = el("preferences").value; data.pending = null; };
    if (el("author")) {
      const select = el("author");
      let listed = [];
      const switchAuthor = () => {
        authors.set(services.projectId, select.value);
        if (select.value) select.name = "input:author_id";
        else select.removeAttribute("name");
        data = draft(`${services.projectId}:${select.value || "shared"}`);
        el("purpose").value = data.purpose;
        el("preferences").value = data.preferences;
        el("text").value = data.text || "";
        const author = listed.find(a => a.id === select.value);
        el("author-note").textContent = author
          ? `After approval, this guide is saved for ${author.display_name}. Other authors' guides stay unchanged.`
          : "After approval, this updates the shared project writing guide.";
        render();
      };
      el("text").oninput = () => { data.text = el("text").value; };
      const choose = safe(async () => {
        select.disabled = true;
        select.dataset.ready = "false";
        switchAuthor();
        try {
          const author = listed.find(a => a.id === select.value);
          if (author) {
            const bound = await TinProjectAuthorInput.selectMember(services, author);
            services.assertCurrent();
            if (!root.isConnected) return;
            select.selectedOptions[0].value = bound.id;
            Object.assign(author, bound);
            switchAuthor();
          }
          select.dataset.ready = "true";
        } finally {if (root.isConnected) select.disabled = false;}
      });
      select.onchange = choose;
      const load = async (chosen) => {
        const response = await TinProjectAuthorInput.members(services);
        services.assertCurrent();
        if (!root.isConnected) return;
        listed = response.authors;
        select.innerHTML = '<option value="">Shared project guide</option>' + listed.map(a => `<option value="${escape(a.id)}">${escape(TinProjectAuthorInput.label(a))}</option>`).join("");
        const selection = chosen ?? authors.get(services.projectId) ?? response.default_author_id ?? "";
        if (selection && !listed.some(a => a.id === selection)) throw new Error("The selected author is unavailable. Refresh this setup and choose an author.");
        select.value = selection;
        select.disabled = false;
        await choose();
      };
      safe(() => load())();
    }
    function render() {
      el("list").innerHTML = data.samples.map((s, i) => `<details><summary>${escape(s.label)} · ${escape(s.kind)}</summary><pre class="style-sample-preview">${escape(s.text)}</pre><button class="button-quiet" type="button" data-style-remove="${i}">Remove</button></details>`).join("");
      el("list").querySelectorAll("[data-style-remove]").forEach((button) => {
        button.onclick = () => { data.samples.splice(Number(button.dataset.styleRemove), 1); data.pending = null; render(); };
      });
    }
    function add(sample) {
      services.assertCurrent();
      if (!sample.text.trim()) throw new Error("Add a non-empty writing passage.");
      if (data.samples.length >= 8) throw new Error("Use up to 8 samples.");
      if (bytes(JSON.stringify([...data.samples, sample])) > 95000) throw new Error("Select shorter passages; the combined samples exceed 100 KB.");
      data.samples.push({...sample, kind: el("kind").value}); data.pending = null; render();
    }
    el("agent").onclick = safe(async () => {
      const selectedAuthor = el("author")?.value;
      const authorContext = selectedAuthor ? ` Call get_writing_style_guide with author_id ${selectedAuthor} and use that author_id for style.capture. Keep this author separate from other project voices.` : "";
      await navigator.clipboard.writeText(`Help me capture a writing style for Tin project ${services.projectId}.${authorContext} Use the guide in get_workflow's preparation for style.capture, or call get_writing_style_guide. Start with one short invitation for pieces that sound like me, including blog posts I've written as links or files, unless I already supplied samples. Reuse our conversation's context; do not turn source discovery into a questionnaire. Obsidian notes and recent Codex, Claude Code or other harness sessions are options when helpful, not extra questions to ask if my writing samples are sufficient. Ask only for missing context or scoped read permission, and help locate sources if needed using your own available tools. Inspect the approved sources, select substantial representative passages and my editing preferences, then show me the selection in one concise confirmation before sharing it with Tin and running style.capture. Honor approval already given. Do not silently substitute a few messages from this conversation. If histories are unavailable, explain that and offer documents or selected exports. Do not treat assistant replies as my writing.`);
      message("Prompt copied. Paste it into your connected coding agent.");
    });
    el("connect").onclick = () => services.openAgent();
    el("add").onclick = safe(() => { add({label: `Passage ${data.samples.length + 1}`, origin: "User-pasted passage", text: el("text").value}); el("text").value = ""; data.text = ""; });
    el("upload").onchange = safe(async () => {
      const files = [...el("upload").files];
      if (files.length + data.samples.length > 8 || files.reduce((n, f) => n + f.size, 0) > 10 * 1024 * 1024) throw new Error("Choose at most 8 samples and 10 MiB per upload selection.");
      for (const file of files) {
        if (file.size > 5 * 1024 * 1024) throw new Error("Choose files no larger than 5 MiB each.");
        message(`Reading ${file.name}…`);
        const sample = await services.api(`/api/projects/${services.projectId}/writing-style/preview?filename=${encodeURIComponent(file.name)}`, {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file});
        add({label: sample.label, origin: "User-selected upload", text: sample.text});
        message(sample.warnings.join(" ") || "Sample added. Expand it to preview the selected text.");
      }
      el("upload").value = "";
    });
    let inventory;
    el("files").onclick = safe(async () => {
      inventory = await services.api(`/api/projects/${services.projectId}/files`);
      services.assertCurrent();
      const files = inventory.files.filter((f) => /\.(md|txt)$/i.test(f.path) && !f.path.startsWith(".agents/"));
      el("project-file").innerHTML = files.map((f) => `<option>${escape(f.path)}</option>`).join("");
      el("project-file").hidden = el("add-file").hidden = !files.length;
      if (!files.length) message("No Markdown or text samples in project Files yet.");
    });
    el("add-file").onclick = safe(async () => {
      const path = el("project-file").value;
      const response = await services.fetch(`/api/projects/${services.projectId}/files/raw?path=${encodeURIComponent(path)}&revision=${inventory.revision}`);
      add({label: path.split("/").pop(), origin: `${path}@${inventory.revision}`, text: await response.text()});
    });
    render();
  }
  async function prepare(form, services) {
    services.assertCurrent();
    const author = form.querySelector("[data-style-author]");
    if (author && (author.disabled || author.dataset.ready !== "true")) throw new Error("Choose an available author before continuing.");
    const authorId = author?.value;
    const data = draft(author ? `${services.projectId}:${author.value || "shared"}` : services.projectId);
    if (!data.purpose.trim()) throw new Error("Choose the intended writing context.");
    if (!data.samples.length && !data.preferences.trim()) throw new Error("Add samples here, or use your coding agent to capture style.");
    if (form.querySelector("[data-style-text]")?.value.trim()) throw new Error("Add the pasted passage before continuing.");
    const packet = {purpose: data.purpose, preferences: data.preferences, samples: data.samples.map((s, i) => ({...s, id: `s${i + 1}`}))};
    const content = "# Style samples\n\n```json\n" + JSON.stringify(packet, null, 2) + "\n```\n";
    if (bytes(content) > 100000) throw new Error("Select shorter passages; style samples exceed 100 KB.");
    if (!data.pending) {
      const files = await services.api(`/api/projects/${services.projectId}/files`);
      services.assertCurrent();
      const id = crypto.randomUUID();
      data.pending = {request_id: id, expected_revision: files.revision, message: "Select writing samples for style capture", changes: [{operation: "upsert", path: `style/sources/${id}.md`, content}]};
    }
    const pending = data.pending;
    services.assertCurrent();
    await services.api(`/api/projects/${services.projectId}/files/commit`, {method: "POST", body: JSON.stringify(pending)});
    services.assertCurrent();
    if (author?.value !== authorId) throw new Error("Author changed while saving. Check the samples before continuing.");
    if (data.pending !== pending) throw new Error("Samples changed while saving. Review them and continue again.");
    form.elements.namedItem("input:source_path").value = pending.changes[0].path;
  }
  window.TinStyleCapture = {fields, bind, prepare};
})();
