/* Content-specific behavior composed with the shared My system controls. */
(() => {
  const drafts = new Map();
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const select = (marker, value, rows, label) => tinSelectControl(`content:${marker}`, value, rows, label, {[marker]: ""});
  const setting = (label, control) => `<div class="system-setting"><strong>${esc(label)}</strong>${control}</div>`;
  const dateLabel = value => new Date(`${value}T12:00:00Z`).toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"});
  const readinessLabel = value => ({needs_verification: "Verify before writing", ready: "Ready", deferred: "Deferred"}[value] || "Verify before writing");
  function resultLabel(summary) {
    return ({
      "Content roadmap. Nothing generated or published.": "Roadmap saved",
      "Content batch prepared. Nothing generated or published.": "Batch prepared",
      "Content revision preview. Nothing generated or published.": "Revision ready to review",
      "No content batch due. Nothing generated or published.": "No batch due",
      "Content program finished. Nothing generated or published.": "Program finished",
    })[summary] || summary;
  }
  const durations = [["2_weeks", "2 weeks"], ["1_month", "1 month"], ["2_months", "2 months"], ["3_months", "3 months"], ["6_months", "6 months"]];
  function endDate(start, duration) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) return "Choose a start date";
    const [year, month, day] = start.split("-").map(Number);
    const date = duration === "2_weeks" ? new Date(Date.UTC(year, month - 1, day + 14)) : (() => {
      const months = Number(duration.split("_")[0]);
      const last = new Date(Date.UTC(year, month - 1 + months + 1, 0)).getUTCDate();
      return new Date(Date.UTC(year, month - 1 + months, Math.min(day, last)));
    })();
    return Number.isNaN(date.valueOf()) ? "Choose a duration" : date.toISOString().slice(0, 10);
  }

  function filePicker(name, values = []) {
    return `<div class="content-file-picker"><input type="hidden" name="${esc(name)}" data-content-files value="${esc(JSON.stringify(values))}">
      <div class="content-file-chips" data-file-chips></div><div class="content-file-choice"><div data-file-options>${select("data-file-choice", "", [["", "Choose a project file…"]], "Select a project file")}</div>
      <button type="button" class="system-action" data-add-context>Add file</button></div></div>`;
  }

  function fields(values = {}, locked = false) {
    const research = ["audit", "keyword"].map(kind => setting(kind === "audit" ? "Source audit" : "Source keyword plan", `<div data-content-source="${kind}" data-selected="${esc(values[`${kind}_run_id`] || "")}" ${locked ? "data-source-locked" : ""}>
      ${locked ? `<input type="hidden" name="input:${kind}_run_id" value="${esc(values[`${kind}_run_id`])}"><span class="system-config-note">Loading saved research…</span>` : tinSelectControl(`input:${kind}_run_id`, "", [["", "Loading successful research…"]], kind === "audit" ? "Source audit" : "Source keyword plan")}</div>`)).join("");
    const dates = locked
      ? `<input type="hidden" name="input:start_date" value="${esc(values.start_date)}"><input type="hidden" name="input:duration" value="${esc(values.duration)}"><p class="system-config-note">${esc(dateLabel(values.start_date))} → ${esc(dateLabel(endDate(values.start_date, values.duration)))}</p>`
      : `<label class="system-setting"><strong>Program start</strong><input class="workflow-inline-input" type="date" name="input:start_date" required value="${esc(values.start_date || new Date().toLocaleDateString("en-CA"))}"></label>${setting("Duration", tinSelectControl("input:duration", values.duration || "6_months", durations, "Duration"))}`;
    const scope = `${research}${dates}<p class="system-config-note" data-content-end-date></p>`;
    return `${locked ? `<details class="content-disclosure"><summary>Research and program dates</summary><div class="content-fields">${scope}<p class="system-config-note">Pinned from the first run. Create a new program to change these.</p></div></details>` : scope}
      ${setting("Maximum pieces per batch", tinCounterControl("input:pieces_per_batch", values.pieces_per_batch || 2, {type: "integer", minimum: 1, maximum: 3}, "Maximum pieces per batch"))}
      <details class="content-disclosure"><summary>Project context</summary><div class="content-fields">${filePicker("input:context_files", values.context_files || [])}<p class="system-config-note">Files for future runs. To change the roadmap, request a revision under Upcoming work.</p></div></details>
      <input type="hidden" name="input:amendment_id" value="">
      <p class="system-config-note">Capacity is a ceiling, not a quota. No articles are generated or published.</p>`;
  }

  async function bindPickers(root, context) {
    const {api, projectId, openFile} = context;
    for (const end of root.querySelectorAll("[data-content-end-date]")) {
      if (end.dataset.bound) continue;
      end.dataset.bound = "true";
      const form = end.closest("form"), start = form.elements["input:start_date"], duration = form.elements["input:duration"];
      const update = () => {end.textContent = `Program ends ${endDate(start.value, duration.value)}. Scheduled preparation stops on this date.`;};
      start.addEventListener("change", update); duration.addEventListener("change", update); update();
    }
    const sources = [...root.querySelectorAll("[data-content-source]")];
    if (sources.length) {
      let rows = [], offset = 0;
      do {
        const result = await api(`/api/projects/${projectId}/content-programs/sources?offset=${offset}`);
        rows.push(...result.sources); offset = result.next_offset;
      } while (offset !== null && offset <= 10000 && root.isConnected);
      for (const field of sources) {
        if (!field.isConnected) continue;
        const expected = field.dataset.contentSource === "audit" ? "organic.audit" : "organic.keyword_plan";
        const available = rows.filter(row => row.executor === expected);
        const label = row => `${row.site_url || "Website"} · ${row.market || ""} · ${new Date(row.created_at).toLocaleDateString()}`;
        if (field.hasAttribute("data-source-locked")) {
          const selected = available.find(row => row.id === field.dataset.selected);
          field.innerHTML = `<input type="hidden" name="input:${field.dataset.contentSource}_run_id" value="${esc(field.dataset.selected)}">${selected ? `<button type="button" class="system-action content-source-link">${esc(label(selected))} ↗</button>` : '<span class="system-config-note">Saved research is unavailable.</span>'}`;
          field.querySelector("button")?.addEventListener("click", () => context.openRun?.(selected.id));
        } else {
          field.innerHTML = tinSelectControl(`input:${field.dataset.contentSource}_run_id`, field.dataset.selected, [["", "Select completed research…"], ...available.map(row => [row.id, label(row)])], field.dataset.contentSource === "audit" ? "Source audit" : "Source keyword plan");
        }
        bindTinControls(field);
      }
    }
    if (!root.querySelector(".content-file-picker")) return;
    const listing = await api(`/api/projects/${projectId}/files`);
    for (const picker of root.querySelectorAll(".content-file-picker")) {
      if (picker.dataset.bound) continue;
      picker.dataset.bound = "true";
      const input = picker.querySelector("[data-content-files]");
      picker.querySelector("[data-file-options]").innerHTML = select("data-file-choice", "", [["", "Choose a project file…"], ...listing.files.map(file => [file.path, file.path])], "Select a project file");
      const choice = picker.querySelector("[data-file-choice]");
      bindTinControls(picker);
      const draw = () => {
        const paths = JSON.parse(input.value || "[]");
        picker.querySelector("[data-file-chips]").innerHTML = paths.map((path, index) => `<span class="content-file-chip"><button type="button" data-read-context="${index}">${esc(path)} ↗</button><button type="button" aria-label="Remove ${esc(path)}" data-remove-context="${index}">×</button></span>`).join("");
        picker.querySelectorAll("[data-remove-context]").forEach(button => button.onclick = () => {paths.splice(Number(button.dataset.removeContext), 1); input.value = JSON.stringify(paths); input.dispatchEvent(new Event("change", {bubbles: true})); draw();});
        picker.querySelectorAll("[data-read-context]").forEach(button => button.onclick = () => openFile(paths[Number(button.dataset.readContext)]));
      };
      picker.querySelector("[data-add-context]").onclick = () => {
        const paths = JSON.parse(input.value || "[]");
        if (choice.value && !paths.includes(choice.value) && paths.length < 8) paths.push(choice.value);
        input.value = JSON.stringify(paths); input.dispatchEvent(new Event("change", {bubbles: true})); draw();
      };
      draw();
    }
  }

  function changes(before, after) {
    const index = plan => new Map(plan.batches.flatMap(batch => batch.items.map(item => [item.id, {batch: batch.id, item}])));
    const old = index(before), next = index(after);
    return {
      added: [...next.keys()].filter(id => !old.has(id)).length,
      removed: [...old.keys()].filter(id => !next.has(id)).length,
      moved: [...next].filter(([id, row]) => old.has(id) && old.get(id).batch !== row.batch).length,
      edited: [...next].filter(([id, row]) => old.has(id) && JSON.stringify(old.get(id).item) !== JSON.stringify(row.item)).length,
    };
  }

  async function mount(root, context) {
    bindPickers(root, context).catch(error => context.toast(error.message));
    for (const panel of root.querySelectorAll("[data-content-program]")) {
      const id = panel.dataset.contentProgram, key = `${context.projectId}:${id}`;
      let draft = drafts.get(key);
      const base = `/api/projects/${context.projectId}/content-programs/${id}`;
      const projection = panel.dataset.contentProjection;
      const reload = async () => {
        panel.textContent = "Loading content program…";
        try {
          const facts = await context.api(base);
          const read = facts.initialized ? await context.api(`${base}/plan`) : null;
          draft = {facts, read, selected: draft?.selected || null, openTopic: draft?.openTopic || null, instruction: "", contextPaths: [], scope: "selected", dirty: false, projection};
          drafts.set(key, draft);
          if (panel.isConnected) draw();
        } catch (error) {
          panel.innerHTML = `<p role="alert">${esc(error.message)}</p><button class="button-quiet" type="button" data-reload>Retry</button>`;
          panel.querySelector("[data-reload]").onclick = reload;
        }
      };
      const draw = () => {
        if (!panel.isConnected) return;
        if (!draft.read) {
          panel.innerHTML = '<p class="system-config-note">Run this saved program to create its editable roadmap.</p><button type="button" class="system-action" data-refresh-program>Refresh program</button>';
          panel.querySelector("[data-refresh-program]").onclick = reload; return;
        }
        const plan = draft.read.plan, facts = draft.facts;
        const reserved = new Set(facts.batches.map(row => row.batch_id));
        const held = new Set(facts.pending_revision?.batch_ids || []);
        const future = plan.batches.filter(batch => !reserved.has(batch.id));
        const batch = plan.batches.find(row => row.id === draft.selected) || future[0] || plan.batches[0];
        draft.selected = batch.id;
        const reservation = facts.batches.find(row => row.batch_id === batch.id);
        const editable = !reserved.has(batch.id) && !held.has(batch.id);
        const totalItems = plan.batches.reduce((total, row) => total + row.items.length, 0);
        const emptyWeeks = plan.batches.filter(row => !row.items.length).length;
        panel.innerHTML = `<header class="content-plan-header"><strong>Upcoming work</strong><button type="button" class="system-action is-strong" data-full-plan>Open full plan ↗</button></header>
          <p class="system-config-note">${totalItems} ${totalItems === 1 ? "topic" : "topics"} · ${plan.batches.length} weeks${emptyWeeks ? ` · ${emptyWeeks} weeks unplanned` : ""}</p>
          <p class="system-config-note">Edits apply only to batches that haven't started.</p>
          <form data-content-plan-form>
          ${setting("Batch", select("data-batch-select", batch.id, plan.batches.map(row => {
            const started = facts.batches.find(record => record.batch_id === row.id);
            const count = started ? started.item_ids.length : row.items.length;
            return [row.id, `${dateLabel(row.due_date)} · ${count} ${count === 1 ? "topic" : "topics"}${started ? started.status === "prepared" ? " · prepared" : " · reserved" : held.has(row.id) ? " · held for revision" : ""}`];
          }), "Batch"))}
          ${reservation ? `<p class="system-config-note">This batch has started. Its saved snapshot stays unchanged.</p><button type="button" class="system-action is-strong" data-read-prepared ${reservation.status === "prepared" ? "" : "disabled"}>Read prepared batch ↗</button>` : ""}
          ${held.has(batch.id) ? '<p class="system-config-note" role="status">This batch is held for revision. Review or discard the proposal below to release it.</p>' : ""}
          <div class="content-batch-items">${reservation ? "" : batch.items.map((item, index) => `<details class="content-disclosure content-topic" data-topic-id="${esc(item.id)}" ${draft.openTopic === item.id ? "open" : ""}>
            <summary><strong>${esc(item.title)}</strong><span>${esc(readinessLabel(item.readiness))}</span></summary>
            ${facts.drafts?.[item.id]?.assessment ? `<p class="system-config-note">Previous assessment: ${esc({already_covered: "Already covered", needs_replanning: "Brief needs revision", insufficient_evidence: "Coverage needs checking", assessment_saved: "Saved assessment; recheck this brief"}[facts.drafts[item.id].stage])}. ${esc(facts.drafts[item.id].assessment.rationale)} ${/^https:\/\//.test(facts.drafts[item.id].covered_by || "") ? `Covered by <a href="${esc(facts.drafts[item.id].covered_by)}" target="_blank" rel="noopener noreferrer">${esc(facts.drafts[item.id].covered_by.replace(/^https:\/\/[^/]+/, "") || "/")}</a>; the next draft moves on. ` : ""}<a class="system-action is-strong" href="/document/${encodeURIComponent(facts.drafts[item.id].run_id)}?project=${encodeURIComponent(context.projectId)}&return=workflows${facts.drafts[item.id].output_source === "retained" ? "&source=retained" : ""}">Read assessment →</a></p>` : ""}
            <fieldset data-item="${index}" aria-label="Edit ${esc(item.title)}" ${editable ? "" : "disabled"}>
            <label class="system-setting"><strong>Topic</strong><input class="workflow-inline-input" data-item-field="title" value="${esc(item.title)}" maxlength="180" required></label>
            <label class="system-setting"><strong>Short brief</strong><textarea class="workflow-inline-input workflow-inline-textarea" data-item-field="brief" maxlength="1800" required>${esc(item.brief)}</textarea></label>
            ${setting("Action", tinSelectControl(`content:action:${index}`, item.action, [["new_page","New page"],["update_page","Update existing page"]], "Action", {"data-item-field":"action"}))}
            <label class="system-setting"><strong>Destination URL</strong><input class="workflow-inline-input is-literal" data-item-field="destination" type="url" value="${esc(item.destination)}" placeholder="https://${esc(plan.host)}/…"></label>
            <details class="content-disclosure content-topic-details"><summary>Intent and verification</summary><div class="content-fields">
              <label class="system-setting"><strong>Buyer intent</strong><input class="workflow-inline-input" data-item-field="intent" value="${esc(item.intent)}" maxlength="500" required></label>
              <ul class="content-verification">${item.verification.map(value => `<li>${esc(value)}</li>`).join("")}</ul></div></details>
            <div class="content-topic-actions">${setting("Move to", tinSelectControl(`content:move:${index}`, batch.id, future.filter(row => !held.has(row.id)).map(row => [row.id, dateLabel(row.due_date)]), "Move topic to batch", {"data-move-item": index}))}
              <button type="button" class="system-action" data-defer-item="${index}">Defer one batch</button>
              <button type="button" class="system-action" data-remove-item="${index}">Remove topic</button></div>
            </fieldset></details>`).join("") || (reservation ? "" : '<p class="system-config-note">No work assigned. Add a topic or request a revision.</p>')}</div>
          <button class="system-action is-strong" type="button" data-add-item ${!editable || batch.items.length >= 3 ? "disabled" : ""}>Add topic +</button>
          <footer class="system-config-footer content-plan-actions"><button class="button" type="submit" data-save-plan ${!draft.dirty ? "disabled" : ""}>Save upcoming work</button>
            <button type="button" class="button-quiet" data-refresh-program>${draft.dirty ? "Discard unsaved edits" : "Refresh program"}</button></footer>
          </form>
          <details class="content-disclosure content-revision" ${facts.pending_revision || draft.revisionOpen ? "open" : ""}><summary>Ask for a revision</summary><div class="content-fields">
            ${facts.pending_revision ? `<p class="system-config-note" role="status">${facts.pending_revision.batch_ids.length} future batches held for revision.</p><div data-preview-summary></div>
              <div class="system-config-footer content-plan-actions">
              <button type="button" class="button-secondary" data-view-preview ${facts.pending_revision.preview_revision ? "" : "disabled"}>Review preview</button>
              <button type="button" class="button" data-apply-preview disabled>Apply to upcoming work</button>
              <button type="button" class="button-quiet" data-discard-preview>Discard</button></div>
              <p class="system-config-note">${facts.pending_revision.preview_revision ? "Review both versions before applying. No prepared batch will change." : "The preview run is preparing a proposal. Discard this request to release the hold."}</p>` : `<form data-content-revision-form class="content-fields">
              <label class="system-setting"><strong>What should change?</strong><textarea class="workflow-inline-input workflow-inline-textarea" data-revision-instruction maxlength="4000" required>${esc(draft.instruction)}</textarea></label>
              ${setting("Applies to", select("data-revision-scope", draft.scope, [["selected","Selected batch"],["next","Next upcoming batch"],["remaining","All remaining batches"]], "Revision scope"))}
              ${setting("Reference files", filePicker("revision-context", draft.contextPaths))}<footer class="system-config-footer content-plan-actions"><button type="submit" class="button-secondary" data-request-revision>Preview revision</button></footer>
              <p class="system-config-note">Creates a proposal for review. Nothing changes until you apply it.</p></form>`}</div></details>
          <p class="system-config-note" data-plan-message role="status"></p>
          <div data-content-delivery-program="${esc(id)}"></div>`;
        bindTinControls(panel);
        window.TinContentDelivery?.mount(panel, context, {plan, drafts: facts.drafts || {}});
        panel.querySelectorAll(".content-topic").forEach(topic => topic.ontoggle = () => {
          if (topic.open) draft.openTopic = topic.dataset.topicId;
          else if (draft.openTopic === topic.dataset.topicId) draft.openTopic = null;
        });
        panel.querySelector(".content-revision").ontoggle = event => {draft.revisionOpen = event.currentTarget.open;};
        const message = text => {panel.querySelector("[data-plan-message]").textContent = text;};
        const dirty = () => {draft.dirty = true; draft.saveRequest = null; panel.querySelector("[data-save-plan]").disabled = false;};
        const act = async (button, fn) => {button.disabled = true; try {await fn();} catch (error) {message(error.message); button.disabled = false;}};
        panel.querySelector("[data-full-plan]").onclick = () => context.openFile(facts.plan_path);
        panel.querySelector("[data-refresh-program]").onclick = reload;
        if (reservation) panel.querySelector("[data-read-prepared]").onclick = () => context.openRun(reservation.run_id);
        panel.querySelector("[data-batch-select]").onchange = event => {draft.selected = event.target.value; draft.revisionRequest = null; draw();};
        panel.querySelectorAll("[data-item-field]").forEach(field => {
          const update = () => {batch.items[Number(field.closest("[data-item]").dataset.item)][field.dataset.itemField] = field.value; dirty();};
          field.oninput = update; field.onchange = update;
        });
        const move = (index, targetId) => {
          const target = future.find(row => row.id === targetId);
          if (!editable || !target || held.has(target.id) || target.id === batch.id || target.items.length >= 3) {message("Choose another editable batch with room."); return;}
          target.items.push(...batch.items.splice(index, 1)); dirty(); draw();
        };
        panel.querySelectorAll("[data-move-item]").forEach(field => field.onchange = () => move(Number(field.dataset.moveItem), field.value));
        panel.querySelectorAll("[data-defer-item]").forEach(button => button.onclick = () => move(Number(button.dataset.deferItem), future.find(row => row.due_date > batch.due_date && !held.has(row.id))?.id));
        panel.querySelectorAll("[data-remove-item]").forEach(button => button.onclick = () => {batch.items.splice(Number(button.dataset.removeItem), 1); dirty(); draw();});
        panel.querySelector("[data-add-item]").onclick = () => {
          const id = `topic_${crypto.randomUUID().replaceAll("-", "")}`;
          batch.items.push({id, title: "New topic", brief: "Describe the useful answer.", intent: "Describe the buyer's question.", action: "new_page", destination: "", source_ids: [], verification: ["Verify facts and existing page coverage before generation."], readiness: "needs_verification"});
          draft.openTopic = id; dirty(); draw();
          panel.querySelector(`[data-topic-id="${id}"] [data-item-field="title"]`).focus();
        };
        panel.querySelector("[data-content-plan-form]").onsubmit = event => {
          event.preventDefault(); event.stopPropagation();
          if (!draft.dirty) return;
          act(panel.querySelector("[data-save-plan]"), async () => {
          draft.saveRequest ||= crypto.randomUUID();
          await context.api(`${base}/plan`, {method: "PUT", body: JSON.stringify({request_id: draft.saveRequest, expected_revision: draft.read.revision, plan})});
          await reload(); context.toast("Upcoming content saved.");
          });
        };
        if (!facts.pending_revision) {
          panel.querySelector("[data-revision-instruction]").oninput = event => {draft.instruction = event.target.value; draft.revisionRequest = null;};
          panel.querySelector("[data-revision-scope]").onchange = event => {draft.scope = event.target.value; draft.revisionRequest = null;};
          panel.querySelector("[data-content-files]").onchange = event => {draft.contextPaths = JSON.parse(event.target.value); draft.revisionRequest = null;};
          panel.querySelector("[data-content-revision-form]").onsubmit = event => {
            event.preventDefault(); event.stopPropagation();
            act(panel.querySelector("[data-request-revision]"), async () => {
            if (draft.dirty) throw new Error("Save your direct edits before asking for a revision.");
            const batchIds = draft.scope === "remaining" ? future.map(row => row.id) : [draft.scope === "next" ? future[0]?.id : batch.id];
            if (!batchIds.length || batchIds.some(id => !future.some(row => row.id === id))) throw new Error("Choose a batch that has not started.");
            draft.revisionRequest ||= crypto.randomUUID();
            await context.api(`${base}/revisions`, {method: "POST", body: JSON.stringify({request_id: draft.revisionRequest, expected_revision: draft.read.revision, batch_ids: batchIds, instruction: draft.instruction, context_paths: draft.contextPaths})});
            await reload(); context.toast("Revision started; selected batches are held."); context.poll();
            });
          };
          bindPickers(panel, context).catch(error => message(error.message));
        } else {
          const pending = facts.pending_revision;
          panel.querySelector("[data-view-preview]").onclick = event => act(event.currentTarget, async () => {
            if (draft.dirty) throw new Error("Save or reload your local edits before reviewing a preview.");
            const current = await context.api(`${base}/plan`);
            if (JSON.stringify(current.plan) !== JSON.stringify(plan)) throw new Error("The working plan changed. Refresh the program before reviewing this preview.");
            draft.read.revision = current.revision;
            const query = new URLSearchParams({path: `reports/content-plan/${pending.run_id}/plan.json`, revision: pending.preview_revision});
            const proposed = await context.api(`/api/projects/${context.projectId}/files/raw?${query}`);
            const summary = changes(plan, proposed);
            panel.querySelector("[data-preview-summary]").innerHTML = `<p>${summary.added} added · ${summary.removed} removed · ${summary.moved} moved · ${summary.edited} edited</p>` + pending.batch_ids.map(id => {
              const before = plan.batches.find(row => row.id === id), after = proposed.batches.find(row => row.id === id);
              const describe = batch => batch.items.map(item => `${item.title}\n${item.brief}\nIntent: ${item.intent}\n${item.action}: ${item.destination}\nReadiness: ${item.readiness}\nVerify: ${item.verification.join("; ")}\nSources: ${item.source_ids.join(", ")}\nID: ${item.id}`).join("\n\n") || "No work assigned.";
              return `<details open><summary>${esc(before.due_date)}</summary><div class="content-preview-columns"><div><strong>Current plan</strong><pre>${esc(describe(before))}</pre></div><div><strong>Proposed plan</strong><pre>${esc(describe(after))}</pre></div></div></details>`;
            }).join("") + `<details><summary>Overall strategy</summary><div class="content-preview-columns"><pre>${esc(plan.strategy)}</pre><pre>${esc(proposed.strategy)}</pre></div></details>`;
            panel.querySelector("[data-apply-preview]").disabled = false;
          });
          for (const [selector, action] of [["[data-apply-preview]", "apply"], ["[data-discard-preview]", "discard"]]) {
            panel.querySelector(selector).onclick = event => act(event.currentTarget, async () => {
              await context.api(`${base}/revisions/${pending.id}`, {method: "POST", body: JSON.stringify({action, expected_revision: draft.read.revision})});
              await reload(); context.toast(`Revision ${action === "apply" ? "applied" : "discarded"}.`);
            });
          }
        }
      };
      if (draft) {
        if (projection !== draft.projection) {
          try {
            draft.facts = await context.api(base); draft.projection = projection;
            // The already-open editor may obtain its first plan when initial planning ends.
            // Later polling never replaces a user's existing draft with a moving file.
            if (!draft.read && draft.facts.initialized) draft.read = await context.api(`${base}/plan`);
          } catch (error) {context.toast(error.message);}
        }
        draw();
      } else await reload();
    }
  }
  window.TinContentPlan = {fields, mount, changes, endDate, resultLabel, filePicker, bindPickers};
})();
