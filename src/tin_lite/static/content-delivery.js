/* Program delivery uses the existing work-column disclosure and shared controls. */
(() => {
  const pending = new Map();
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const field = (label, content) => `<label class="system-setting"><strong>${esc(label)}</strong>${content}</label>`;
  const input = (name, value, label) => `<input class="workflow-inline-input" data-delivery-field="${name}" value="${esc(value)}" aria-label="${esc(label)}">`;
  const labels = {awaiting_review: "Awaiting review", pending: "PR delivery queued", started: "Opening PR", failed: "PR delivery needs attention", completed: "PR opened"};
  const safePR = value => typeof value === "string" && /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/.test(value);
  async function mount(root, context, {plan, drafts}) {
    for (const panel of root.querySelectorAll("[data-content-delivery-program]")) {
      if (panel.dataset.bound) continue;
      panel.dataset.bound = "true";
      const id = panel.dataset.contentDeliveryProgram;
      const key = `${context.projectId}:${id}`;
      const base = `/api/projects/${context.projectId}/content-programs/${id}/delivery`;
      const items = plan.batches.flatMap(b => b.items);
      let state = pending.get(key);
      const draw = () => {
        if (!panel.isConnected) return;
        const saved = state.settings;
        const latest = Object.entries(drafts).filter(([, row]) => row.has_output);
        panel.innerHTML = `<details class="content-disclosure" data-delivery-disclosure ${state.open ? "open" : ""}><summary>Article delivery · ${saved.mode === "github_pr" ? "GitHub PR" : "Drafts in Tin"}</summary>
          <form class="content-fields" data-delivery-form>
          ${field("After approval", tinSelectControl("delivery:mode", saved.mode, [["draft_only", "Keep drafts in Tin"], ["github_pr", "Open a GitHub PR"]], "After approval", {"data-delivery-field": "mode"}))}
          <div class="content-fields" data-github-settings ${saved.mode === "github_pr" ? "" : "hidden"}>
          ${field("Repository", input("repository", saved.repository, "Repository"))}
          <p class="system-config-note">Use the repository selected in <a href="/integrations?project=${encodeURIComponent(context.projectId)}" class="system-action">Integrations</a>. Pull-request access must be enabled.</p>
          ${field("New article files", input("path_pattern", saved.path_pattern, "New article files"))}
          <p class="system-config-note">For example: content/blog/{slug}.md. Markdown only; no website is published.</p>
          <details class="content-disclosure"><summary>Site frontmatter and existing-page files</summary><div class="content-fields">
          ${field("Frontmatter for new files", `<textarea class="workflow-inline-input workflow-inline-textarea" data-frontmatter rows="4" aria-label="Frontmatter for new files">${esc(state.frontmatter)}</textarea>`)}
          <p class="system-config-note">Optional JSON fields, for example {"title":"{title}","date":"{date}"}. Existing files keep their frontmatter.</p>
          ${items.filter(item => item.action === "update_page" || saved.item_paths[item.id]).map(item => field(item.title, `<input class="workflow-inline-input" data-item-path="${esc(item.id)}" value="${esc(saved.item_paths[item.id] || "")}" placeholder="Exact repository file, such as content/docs/setup.md" aria-label="File for ${esc(item.title)}">`)).join("")}
          <p class="system-config-note">Map each existing-page update to its actual Markdown file before drafting for PR delivery.</p>
          </div></details></div>
          <p class="system-config-note">Applies to new drafts only. Their review action will say “Approve & open PR”. Existing drafts keep their original review behavior. Nothing is merged.</p>
          <footer class="system-config-footer content-plan-actions"><button class="button-secondary" type="submit" data-save-delivery ${state.dirty ? "" : "disabled"}>Save delivery</button><button class="button-quiet" type="button" data-reload-delivery>Reload saved settings</button></footer>
          <p class="system-config-note" role="status" data-delivery-message></p>
          </form></details>
          ${latest.length ? `<details class="content-disclosure"><summary>Drafts and pull requests · ${latest.length}</summary><div class="content-fields">${latest.map(([itemId, row]) => {
            const delivery = row.delivery, pr = delivery?.pull_request, adapted = row.repository_delivery;
            const systemDelivery = row.system_delivery?.mode === "github_pr" ? row.system_delivery : null;
            const title = items.find(i => i.id === itemId)?.title || "Saved article";
            const repositoryAction = adapted ? `<p class="system-config-note">${esc(adapted.summary || (adapted.status === "succeeded" ? "Article PR ready" : adapted.status === "failed" ? "Article PR needs attention" : "Preparing article PR"))} · ${esc(adapted.repository)}</p>${safePR(adapted.pull_request_url) ? `<a class="system-action is-strong" href="${esc(adapted.pull_request_url)}" target="_blank" rel="noopener noreferrer">Open article PR ↗</a>` : `<button class="system-action" type="button" data-read-draft="${esc(adapted.run_id)}">View delivery →</button>${adapted.status === "failed" && adapted.has_checkpoint ? `<button class="system-action" type="button" data-retry-delivery="${esc(adapted.run_id)}">Retry</button>` : ""}`}` :
              systemDelivery ? `<p class="system-config-note">${row.status === "succeeded" ? "The organic system is preparing the article PR" : "After approval, the organic system will prepare the article PR"} · ${esc(systemDelivery.binding.repository)}. Nothing is merged.</p>` :
              !delivery && row.status === "succeeded" && state.available_repository ? `<p class="system-config-note">Adapt the approved copy to ${esc(state.available_repository)} as a pull request. Keep the Markdown original in Tin.</p><button class="system-action is-strong" type="button" data-prepare-article-pr="${esc(row.run_id)}">Prepare PR →</button>` : "";
            const retryAdaptation = adapted?.status === "failed" && !adapted.has_checkpoint && !adapted.pull_request_url && adapted.repository === state.available_repository ? `<p class="system-config-note">No saved patch. Trying again starts a new metered adaptation of the same approved article.</p><button class="system-action" type="button" data-prepare-article-pr="${esc(row.run_id)}" data-retry-adaptation="${esc(adapted.run_id)}">Retry</button>` : "";
            return `<div class="system-setting"><strong>${esc(title)}</strong><div><button class="system-action is-strong" type="button" data-read-draft="${esc(row.run_id)}">Read draft →</button>${delivery ? `<p class="system-config-note">${esc(labels[delivery.status] || "Delivery pending")} · ${esc(delivery.repository)} · ${esc(delivery.path)}</p>${pr && safePR(pr.url) ? `<a class="system-action is-strong" href="${esc(pr.url)}" target="_blank" rel="noopener noreferrer">Open PR #${esc(pr.number)} ↗</a>` : ""}${delivery.status === "failed" ? `<p class="system-config-note">${esc(delivery.error)}</p><button class="system-action" type="button" data-retry-delivery="${esc(row.run_id)}">Retry</button>` : ""}` : `<p class="system-config-note">${row.stage === "awaiting_review" ? "Awaiting review" : "Draft saved"} · Markdown in Tin</p>`}${repositoryAction}${retryAdaptation}</div></div>`;
          }).join("")}<button type="button" class="system-action" data-refresh-delivery-status>Refresh delivery status</button></div></details>` : ""}`;
        bindTinControls(panel);
        const message = text => {panel.querySelector("[data-delivery-message]").textContent = text;};
        const changed = () => {state.dirty = true; state.request = null; panel.querySelector("[data-save-delivery]").disabled = false; pending.set(key, state);};
        panel.querySelector("[data-delivery-disclosure]").ontoggle = e => {state.open = e.currentTarget.open;};
        panel.querySelectorAll("[data-delivery-field]").forEach(control => control.oninput = control.onchange = () => {
          saved[control.dataset.deliveryField] = control.value; changed();
          panel.querySelector("[data-github-settings]").hidden = saved.mode !== "github_pr";
        });
        panel.querySelector("[data-frontmatter]").oninput = e => {state.frontmatter = e.target.value; changed();};
        panel.querySelectorAll("[data-item-path]").forEach(control => control.oninput = () => {
          if (control.value.trim()) saved.item_paths[control.dataset.itemPath] = control.value.trim();
          else delete saved.item_paths[control.dataset.itemPath]; changed();
        });
        panel.querySelector("[data-delivery-form]").onsubmit = async e => {
          e.preventDefault(); e.stopPropagation();
          const button = panel.querySelector("[data-save-delivery]"); button.disabled = true;
          try {
            saved.frontmatter = JSON.parse(state.frontmatter || "{}");
            state.request ||= crypto.randomUUID();
            const result = await context.api(base, {method: "PUT", body: JSON.stringify({request_id: state.request, expected_revision: state.revision, settings: saved})});
            state.revision = result.revision; state.dirty = false; state.request = null; draw();
            context.toast("Delivery saved for future drafts.");
          } catch (error) {message(`${error.message} Your unsaved settings are still here.`); button.disabled = false;}
        };
        panel.querySelector("[data-reload-delivery]").onclick = () => {pending.delete(key); load();};
        panel.querySelectorAll("[data-read-draft]").forEach(b => b.onclick = () => context.openRun(b.dataset.readDraft));
        panel.querySelectorAll("[data-prepare-article-pr]").forEach(b => b.onclick = async () => {
          b.disabled = true;
          const source = b.dataset.prepareArticlePr;
          const retry = b.dataset.retryAdaptation;
          try {
            // website.change (source content_draft) puts the approved page on the site; a failed
            // adaptation with nothing delivered is replaced by a fresh one.
            const result = await context.api("/api/workflows/00000000-0000-4000-8000-000000000045/runs", {
              method: "POST", headers: {"Idempotency-Key": `website-change:${source}:${state.available_repository}${retry ? `:${retry}` : ""}`},
              body: JSON.stringify({project_id: context.projectId, inputs: {source: "content_draft", source_run_id: source, expected_repository: state.available_repository}}),
            });
            const row = Object.values(drafts).find(r => r.run_id === source);
            if (row) row.repository_delivery = {run_id: result.id, status: result.status, repository: state.available_repository};
            draw(); context.toast("Preparing the article PR. Your Markdown draft is unchanged.");
          } catch (error) {context.toast(error.message); b.disabled = false;}
        });
        panel.querySelectorAll("[data-retry-delivery]").forEach(b => b.onclick = async () => {
          b.disabled = true;
          try {
            await context.api(`/api/projects/${context.projectId}/content-drafts/${b.dataset.retryDelivery}/delivery/retry`, {method: "POST"});
            context.toast("Delivery queued. The approved draft is unchanged.");
          } catch (error) {context.toast(error.message); b.disabled = false;}
        });
        panel.querySelector("[data-refresh-delivery-status]")?.addEventListener("click", async () => {
          try {drafts = (await context.api(base.replace(/\/delivery$/, ""))).drafts || {}; draw();}
          catch (error) {context.toast(error.message);}
        });
      };
      const load = async () => {
        try {
          const result = await context.api(base);
          if (!panel.isConnected) return;
          state = {...result, frontmatter: JSON.stringify(result.settings.frontmatter, null, 2), open: false, dirty: false};
          pending.set(key, state); draw();
        } catch (error) {if (panel.isConnected) panel.innerHTML = `<p class="system-config-note">Delivery settings unavailable: ${esc(error.message)}. Reopen this card to retry.</p>`;}
      };
      if (state?.dirty) draw(); else await load();
    }
  }
  function fields(inputs = {}) {
    return `<div class="content-fields" data-article-delivery-picker data-selected-article="${esc(inputs.source_run_id || "")}">
      <div data-article-delivery-options>Loading approved articles…</div>
      <input type="hidden" name="input:expected_repository" value="">
      <input type="hidden" name="input:retry_run_id" value="">
      ${field("Site instructions", `<textarea class="workflow-inline-input workflow-inline-textarea" name="input:direction" rows="3" placeholder="Optional site root or routing conventions">${esc(inputs.direction || "")}</textarea>`)}
      <p class="system-config-note" data-article-delivery-repository></p>
      <p class="system-config-note">Keeps the reviewed Markdown in Tin and prepares an unmerged PR. Does not draft another article or publish your website.</p>
    </div>`;
  }
  function bindPickers(root, {api, projectId}) {
    root.querySelectorAll("[data-article-delivery-picker]").forEach(async panel => {
      if (panel.dataset.bound) return;
      panel.dataset.bound = "true";
      try {
        const result = await api(`/api/projects/${projectId}/content-drafts/delivery-sources`);
        if (!panel.isConnected) return;
        const selected = panel.dataset.selectedArticle;
        const options = result.articles.map(row => [row.run_id, row.title]);
        panel.querySelector("[data-article-delivery-options]").innerHTML = field("Approved article", tinSelectControl("input:source_run_id", selected, [["", "Choose an approved article…"], ...options], "Approved article"));
        panel.querySelector('[name="input:expected_repository"]').value = result.repository || "";
        panel.querySelector("[data-article-delivery-repository]").textContent = !options.length ? "Read and approve an article in Decisions first." : result.repository ? `Website repository: ${result.repository}. Change it in Integrations.` : "Connect your website repository in Integrations first.";
        bindTinControls(panel);
      } catch (error) {
        if (panel.isConnected) panel.querySelector("[data-article-delivery-options]").textContent = error.message;
      }
    });
  }
  function prepare(form) {
    if (!form.querySelector('[name="input:source_run_id"]')?.value) throw new Error("Choose an approved article first.");
    if (!form.querySelector('[name="input:expected_repository"]')?.value) throw new Error("Connect your website repository in Integrations first.");
  }
  window.TinContentDelivery = {mount, fields, bindPickers, prepare};
})();
