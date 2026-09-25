/* Editorial controls belong to the product shell, never the Markdown document. */
(() => {
  const drafts = new Map();
  const reviews = new Map();
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const keyFor = (projectId, runId) => `${projectId}:${runId}`;
  function close(projectId, runId) {
    const draft = drafts.get(keyFor(projectId, runId));
    if (draft) draft.open = false;
  }
  function token(projectId, runId) { return reviews.get(keyFor(projectId, runId))?.review_token; }

  function mount(host, context) {
    const {api, projectId, runId, reader, openRun, onRevised, toast} = context;
    let disposed = false, comparisonCleanup = null;
    const key = keyFor(projectId, runId);
    const draft = drafts.get(key) || {feedback: "", open: false, request: null};
    drafts.set(key, draft);
    const approval = [...host.querySelectorAll(reader ? ".markdown-context-action:not(.is-secondary)" : "[data-apply-decision]")];
    const originalDisabled = approval.map(button => button.disabled);
    approval.forEach(button => {button.disabled = true;});
    const region = document.createElement("section");
    region.className = `workflow-review${reader ? " is-reader" : " is-decision"}`;
    const article = host.querySelector(".markdown-document");
    if (reader) article?.before(region);
    else host.querySelector(".decision-detail-body")?.append(region);
    const actions = reader ? host.querySelector(".markdown-context-bar") : host.querySelector("footer");
    // The run list may lag the exact review read (for example on a direct link).
    // Keep an unbound action hidden until that authoritative response enables it.
    if (reader && !approval.length) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "markdown-context-action";
      button.textContent = context.approvalLabel || "Approve draft";
      button.hidden = true; button.disabled = true;
      button.onclick = () => context.onApprove(button);
      actions?.append(button); approval.push(button); originalDisabled.push(false);
    }
    const requestButton = document.createElement("button");
    requestButton.type = "button";
    requestButton.className = reader ? "markdown-context-action is-secondary is-review-request" : "button-secondary";
    requestButton.hidden = true;
    if (approval[0]) approval[0].before(requestButton); else actions?.append(requestButton);
    // Reader bar order for a draft with GitHub connected: Request changes, Open a pull
    // request, Publish now. The extra choice follows the approval's visibility rules.
    for (const option of reader ? context.deliveryOptions || [] : []) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "markdown-context-action is-secondary is-delivery-option";
      button.textContent = option.label;
      button.hidden = true; button.disabled = true;
      button.onclick = () => context.onApprove(button, option.delivery);
      if (approval[0]) approval[0].before(button); else actions?.append(button);
      approval.push(button); originalDisabled.push(false);
    }
    let review;
    const redraw = () => {
      if (disposed || !host.isConnected) return;
      requestButton.hidden = !review.can_request_changes;
      requestButton.textContent = draft.open ? "Close feedback" : review.status === "failed" ? "Retry revision" : review.artifact?.assessment ? "Give feedback" : "Request changes";
      requestButton.setAttribute("aria-expanded", String(draft.open));
      approval.forEach((button, i) => {button.hidden = draft.open || !review.can_approve; button.disabled = originalDisabled[i];});
      region.innerHTML = "";
      if (review.documents) {
        const summary = document.createElement("p");
        summary.className = "review-change-summary";
        summary.textContent = review.documents.map(item => `${item.destination}: ${item.change === "unchanged" ? "carried forward unchanged" : item.change}`).join(" · ");
        region.append(summary);
      }
      if (review.conflict) {
        const conflict = document.createElement("p");
        conflict.className = "review-error";
        conflict.textContent = review.conflict;
        region.append(conflict);
      }
      if (review.palette_preview) {
        const preview = document.createElement("div");
        preview.className = "review-palette";
        const title = document.createElement("span");
        title.textContent = "Palette preview"; preview.append(title);
        for (const role of ["paper", "ink", "accent", "signal"]) {
          const color = review.palette_preview[role];
          if (typeof color !== "string" || !/^#[0-9a-f]{6}$/i.test(color)) continue;
          const swatch = document.createElement("span"), chip = document.createElement("i");
          chip.style.backgroundColor = color; chip.setAttribute("aria-hidden", "true");
          swatch.append(chip, document.createTextNode(`${role} ${color}`)); preview.append(swatch);
        }
        region.append(preview);
      }
      if (!review.is_current) {
        region.innerHTML = `<p class="review-version-notice">A newer version is available. <button type="button" class="system-action" data-current-review>Read current version →</button></p>`;
        region.querySelector("[data-current-review]").onclick = () => openRun(review.current_run_id);
        return;
      }
      if (review.version > 1) {
        const versions = document.createElement("div");
        versions.className = "review-version-row";
        const copyLabel = review.artifact?.run_id === runId ? `Version ${esc(review.version)}` : review.status === "failed" ? "Revision failed · previous copy" : review.status === "stopped" ? "Revision stopped · previous copy" : "Revising · previous copy";
        versions.innerHTML = `<span>${copyLabel}</span><button type="button" class="system-action" data-previous>Previous version</button>${review.artifact?.run_id === runId ? '<button type="button" class="system-action" data-review-compare>Compare</button>' : ""}${review.status === "failed" ? '<button type="button" class="system-action" data-stop-revision>Stop revision</button>' : ""}`;
        versions.querySelector("[data-previous]").onclick = () => openRun(review.versions.find(v => v.id !== runId && v.artifact_path)?.id);
        versions.querySelector("[data-review-compare]")?.addEventListener("click", compare);
        versions.querySelector("[data-stop-revision]")?.addEventListener("click", async event => {
          event.currentTarget.disabled = true;
          try {
            await api(`/api/workflows/runs/${runId}/stop-procedure`, {method: "POST"});
            if (disposed) return;
            review.status = "stopped"; review.can_request_changes = false; draft.open = false; redraw();
          } catch (error) {if (!disposed) {toast(error.message); redraw();}}
        });
        region.append(versions);
        if (review.change_summary) {
          const summary = document.createElement("p");
          summary.className = "review-change-summary";
          summary.textContent = `What changed: ${review.change_summary}`;
          region.append(summary);
        }
      }
      if (!draft.open) return;
      const form = document.createElement("form");
      form.className = "review-composer";
      const id = `review-feedback-${runId}`;
      form.innerHTML = `<label for="${esc(id)}">What should change?</label>
        <p>For this draft only. Your feedback won’t change your saved writing style.</p>
        <textarea id="${esc(id)}" name="feedback" maxlength="8000" required placeholder="What should we change, keep, or explain better? Mention any project files to use.">${esc(draft.feedback)}</textarea>
        <p class="review-error" role="alert" hidden></p>
        <footer><button type="submit" class="review-submit">${review.artifact?.assessment ? "Recheck with feedback" : "Revise draft"}</button></footer>`;
      region.append(form);
      form.elements.feedback.oninput = () => {draft.feedback = form.elements.feedback.value; draft.request = null;};
      form.onsubmit = async event => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        const button = form.querySelector("[type=submit]"), errorBox = form.querySelector(".review-error");
        button.disabled = true; button.textContent = "Requesting revision…"; errorBox.hidden = true;
        draft.request ||= crypto.randomUUID();
        try {
          const successor = await api(`/api/workflows/runs/${runId}/request-changes`, {
            method: "POST", body: JSON.stringify({feedback: draft.feedback,
              request_id: draft.request, review_token: review.review_token}),
          });
          draft.open = false;
          if (disposed) return;
          onRevised(successor);
        } catch (error) {
          if (disposed) return;
          errorBox.textContent = error.message; errorBox.hidden = false;
          button.disabled = false; button.textContent = review.artifact?.assessment ? "Recheck with feedback" : "Revise draft";
        }
      };
    };
    requestButton.onclick = () => {
      draft.open = !draft.open; redraw();
      if (draft.open) region.querySelector("textarea")?.focus();
    };
    async function compare() {
      try {
        const renderer = await context.loadRenderer();
        const data = await api(`/api/workflows/runs/${runId}/review/compare`);
        if (disposed) return;
        const model = renderer.buildComparison({complete: true,
          current: {presence: "file", content: data.previous.content}, saved: {content: data.revised.content}});
        draft.open = false; redraw();
        const panel = document.createElement("section"); panel.className = "review-comparison";
        panel.innerHTML = '<header><strong>Previous version → Revised version</strong><button class="system-action" type="button">Close comparison</button></header><div class="review-comparison-lines"></div>';
        region.append(panel);
        comparisonCleanup?.destroy();
        comparisonCleanup = renderer.mountDiff(panel.lastElementChild, model, {editorial: true});
        if (reader) {article.hidden = true; host.querySelector(".markdown-section-map").hidden = true; host.querySelector(".markdown-reader-layout").classList.add("is-review-comparison");}
        panel.querySelector("button").onclick = () => {
          comparisonCleanup?.destroy(); panel.remove();
          if (reader) {article.hidden = false; host.querySelector(".markdown-section-map").hidden = false; host.querySelector(".markdown-reader-layout").classList.remove("is-review-comparison");}
        };
      } catch (error) {if (!disposed) toast(error.message);}
    }
    api(`/api/workflows/runs/${runId}/review`).then(value => {
      if (disposed || !host.isConnected) return;
      review = value; reviews.set(key, review);
      if (review.status === "failed" && !draft.feedback) draft.feedback = review.feedback || "";
      redraw();
    }).catch(error => {
      if (disposed) return;
      // Do not leave an unbound approval active after review state could not be read.
      region.textContent = `Review controls are unavailable: ${error.message}`;
    });
    return () => {disposed = true; comparisonCleanup?.destroy();};
  }
  window.TinWorkflowReview = {mount, token, close};
})();
