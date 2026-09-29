"use strict";

// The page owns presentation only. All choices go through the exact-version service.
window.TinOutputComparisonPage = (() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const short = (revision) => String(revision || "").slice(0, 7);
  const isSettled = (resolution) => ["applied", "kept"].includes(resolution?.state);

  function mount(root, options) {
    const { runId, projectId, actorId, api, loadRenderer, onRead, onReturn, onOutcome, onFiles, onActivity } = options;
    const session = options.session;
    const key = `tin-output-choice:${actorId}:${projectId}:${runId}`;
    const endpoint = `/api/workflows/runs/${encodeURIComponent(runId)}`;
    let alive = true;
    let diff = null;
    let renderer = null;
    let loading = false;
    let sending = false;
    let error = null;
    let stale = false;
    let choice = "keep_current";
    let status = null;
    let comparison = null;
    let model = null;
    let retry = null;
    let uncertain = false;

    function remember(request) {
      localStorage.setItem(key, JSON.stringify(request));
      session.request = request;
    }
    function forget() {
      session.request = null;
      try { localStorage.removeItem(key); } catch { /* Completed server receipt is authoritative. */ }
    }
    function storedRequest() {
      try {
        const request = session.request || JSON.parse(localStorage.getItem(key) || "null");
        if (!request || !/^[0-9a-f-]{36}$/i.test(request.request_id)
            || !["keep_current", "use_saved"].includes(request.action)
            || !/^[0-9a-f]{40}$/.test(request.expected_revision)
            || !/^[0-9a-f]{40}$/.test(request.saved_revision)) return null;
        return request;
      } catch { return null; }
    }
    function canUse() {
      return comparison?.allowed_actions.includes("use_saved") && model && !stale && !uncertain && !error;
    }
    function canKeep() {
      return comparison?.allowed_actions.includes("keep_current") && !stale && !uncertain && !error;
    }
    function button(label, action, extra = "") {
      return `<button type="button" data-compare-action="${action}" ${extra}>${label}</button>`;
    }
    function readLinks() {
      return `${comparison?.current.presence === "file" ? button("Read current →", "read-current") : ""}${button("Read saved result →", "read-saved")}`;
    }
    function footerText() {
      if (sending) return choice === "keep_current" ? "Tin is recording your choice." : "Tin is confirming the change in Files. This can take a moment.";
      if (uncertain) return "Another choice stays unavailable until the outcome is known.";
      if (stale) return "Confirming is paused until the comparison is refreshed.";
      if (loading || error) return "Nothing can be confirmed until the comparison loads.";
      if (comparison?.identical) return "Acknowledging records that and closes this decision. Nothing changes in Files.";
      if (choice === "keep_current") return comparison?.current.presence === "missing"
        ? "Files stay as they are; the file stays missing. The saved result stays with the run."
        : "Nothing changes in Files. The saved result stays with the run.";
      if (comparison?.current.presence === "missing") return `Creates ${comparison.path} from the saved result. Nothing else changes.`;
      return model?.blocks.some((block) => block.marked)
        ? "Using the saved result replaces the whole file, including edits made after the run started."
        : "Using the saved result replaces the whole file.";
    }
    function noticeHtml() {
      if (uncertain) return `<div class="compare-notice" role="status"><p>Tin hasn't confirmed the outcome yet. ${retry ? "Check outcome re-sends the same request; it cannot apply the result twice." : "The original caller must check the outcome. If that caller is unavailable, contact support."}</p><div>${button(retry ? "Check outcome" : "Check status", "check")}${button("Open Files →", "files")}${button("View in Activity →", "activity")}</div></div>`;
      if (stale) return `<div class="compare-notice" role="alert"><p>The project changed while you were reading. This comparison may be out of date.</p>${button("Refresh comparison", "refresh")}</div>`;
      if (error) return `<div class="compare-notice" role="alert"><p>${escape(error)}</p>${button("Retry", "refresh")}</div>`;
      if (loading) return '<div class="compare-notice" role="status">Loading the comparison…</div>';
      if (!comparison) return "";
      if (comparison.identical) return '<div class="compare-notice" role="status">The current file and the saved result are the same. There is nothing to replace.</div>';
      if (!comparison.complete || !model) {
        const message = comparison.blocked_reason === "comparison_limit"
          ? "This comparison is too large to show: the two files together are over 200,000 bytes or 10,000 lines. Read the complete files instead."
          : comparison.blocked_reason === "non_text_destination"
            ? "The current file is not UTF-8 text. You can keep it as it is; using the saved result is unavailable here."
            : comparison.complete
              ? "Tin couldn't compute a complete diff within its limit. Read the complete files instead."
              : "This path can't be written to from here. You can keep it as it is; the saved result stays readable.";
        return `<div class="compare-notice" role="status"><p>${message}</p><div>${readLinks()}</div></div>`;
      }
      let html = "";
      if (comparison.current.presence === "missing") html += `<div class="compare-notice"><p>${escape(comparison.path)} is not in Files right now. The saved result is shown whole below.</p></div>`;
      if (!model.annotationsAvailable) html += `<div class="compare-notice"><p>Tin couldn't check this run's starting version, so changes that would replace later edits are not marked. Read the current file before choosing.</p>${comparison.current.presence === "file" ? button("Read current →", "read-current") : ""}</div>`;
      return html;
    }
    function render() {
      if (!alive) return;
      diff?.destroy();
      diff = null;
      const resolution = status?.resolution || comparison?.resolution;
      const settled = isSettled(resolution);
      const path = comparison?.path || status?.path || options.path || "Saved result";
      const footer = settled
        ? `<p>${resolution.state === "kept" ? "The current file was kept. No file change was made; the saved result remains readable." : resolution.changed ? `The saved result was applied at revision ${escape(short(resolution.revision))}. The file may have changed again since.` : "Nothing needed to change; the current file already matched the saved result."}</p><div>${button("Open file →", "files")}${button("Read saved result →", "read-saved")}${button("View in Activity →", "activity")}${button("Back to decisions", "decisions")}</div>`
        : `<p id="compare-consequence" aria-live="polite">${escape(footerText())}</p>
          ${!comparison?.identical ? `<fieldset class="compare-choice" aria-label="Choose a file version" aria-describedby="compare-consequence" ${loading || sending || uncertain || stale || error ? "disabled" : ""}>
            <label><input type="radio" name="output-choice" value="keep_current" ${choice === "keep_current" ? "checked" : ""} ${!canKeep() ? "disabled" : ""}><span>Keep current</span></label>
            <label><input type="radio" name="output-choice" value="use_saved" ${choice === "use_saved" ? "checked" : ""} ${!canUse() ? "disabled" : ""}><span>Use saved result</span></label>
          </fieldset>` : ""}
          ${!sending ? button("Not now", "return", 'class="button-quiet"') : ""}
          ${button(sending ? "Confirming…" : comparison?.identical ? "Acknowledge" : "Confirm choice", "confirm", `class="compare-confirm" ${sending || loading || !(choice === "keep_current" ? canKeep() : canUse()) ? "disabled" : ""}`)}`;
      root.innerHTML = `<section class="compare-view">
        <header class="compare-context"><button type="button" data-compare-action="return">← ${escape(options.returnLabel || "decisions")}</button><code>${escape(path)}</code>${comparison ? `<span>${escape(comparison.media_type.replace("text/", ""))} · current ${escape(short(comparison.current.revision))} · saved result ${escape(short(runId))}</span>` : ""}</header>
        <div class="compare-page"><div class="compare-column">
          <div class="compare-lead"><h1>${settled ? "File version decided" : "What would change if you use this saved result?"}</h1>
          <p>${settled ? "This decision is separate from the workflow, which remains failed or stopped. It does not approve content or publish anything." : `${escape(options.workflowTitle || "The workflow")} couldn't save its result to this file because the file had changed while it ran. Tin left the file alone and saved the result. This compares the file now with that saved result—not who changed what.${model?.annotationsAvailable ? " The edge bar marks changes that would replace later edits." : ""}`}</p></div>
          <article class="compare-card" aria-busy="${loading || sending}">
            ${settled ? "" : noticeHtml()}
            ${model && !settled && !uncertain && !error && !loading && !comparison?.identical ? `<div class="compare-legend">
              <span>− current file <code>${escape(short(comparison.current.revision))}</code> ${comparison.current.presence === "file" ? button("Read →", "read-current", 'aria-label="Read current file"') : "(missing)"}</span>
              <span>+ saved result ${button("Read →", "read-saved", 'aria-label="Read saved result"')}</span>
              ${model.annotationsAvailable ? '<span class="compare-warning-key">▎ would replace later edits</span>' : ""}
              <span class="compare-navigation"><span data-compare-position aria-live="polite">${model.hunks.length ? "1" : "0"} of ${model.hunks.length}</span>${button("↑", "previous", 'aria-label="Previous change"')}${button("↓", "next", 'aria-label="Next change"')}</span>
            </div><div class="compare-diff ${comparison.media_type === "text/markdown" ? "is-wrapped" : "is-numbered"}" tabindex="0" role="region" aria-label="Current file to saved result comparison"></div>` : ""}
            <footer class="compare-footer${sending || loading ? " is-busy" : ""}">${footer}</footer>
          </article>
        </div></div></section>`;
      const pane = root.querySelector(".compare-diff");
      if (pane) diff = renderer.mountDiff(pane, model, {
        numbered: comparison.media_type !== "text/markdown",
        onNavigate: (current, total) => { root.querySelector("[data-compare-position]").textContent = `${current} of ${total}`; },
      });
    }
    async function load() {
      if (loading || sending) return;
      loading = true;
      error = null;
      stale = false;
      choice = "keep_current";
      render();
      try {
        status = await api(`${endpoint}/output-resolution`);
        if (!alive) return;
        if (status.project_id !== projectId) throw new Error("This result belongs to another project.");
        if (isSettled(status.resolution)) { forget(); uncertain = false; retry = null; return; }
        retry = status.resolution?.state === "applying" ? status.retry_request : storedRequest();
        uncertain = status.resolution?.state === "applying" || Boolean(retry);
        if (uncertain) return;
        comparison = await api(`${endpoint}/output-comparison`);
        if (!alive) return;
        if (comparison.project_id !== projectId) throw new Error("This result belongs to another project.");
        if (comparison.resolution) {
          status = await api(`${endpoint}/output-resolution`);
          retry = status.retry_request;
          uncertain = status.resolution?.state === "applying";
          return;
        }
        model = null;
        if (comparison.complete) {
          try {
            renderer = await loadRenderer();
            model = renderer.buildComparison(comparison);
          } catch { /* Keep current remains available; no partial diff is shown. */ }
        }
      } catch (failure) {
        retry = storedRequest();
        uncertain = Boolean(retry);
        error = uncertain ? null : `Tin couldn't load the comparison. ${failure.message}`;
      } finally {
        loading = false;
        render();
      }
    }
    async function submit(request) {
      if (sending) return;
      sending = true;
      error = null;
      choice = request.action;
      try { remember(request); } catch {
        sending = false;
        error = "Tin couldn't save the recovery request in this browser. Allow site storage, then try again. Nothing was sent.";
        render();
        return;
      }
      render();
      let reload = false;
      try {
        const outcome = await api(`${endpoint}/output-resolution`, { method: "POST", body: JSON.stringify(request) });
        forget();
        status = { ...(status || {}), resolution: outcome };
        retry = null;
        uncertain = false;
        onOutcome(outcome);
      } catch (failure) {
        if (failure.code === "stale_comparison") {
          forget();
          retry = null;
          uncertain = false;
          stale = true;
          status = { ...(status || {}), resolution: null };
        } else if (["resolution_exists", "request_conflict", "wrong_saved_version", "comparison_blocked"].includes(failure.code)) {
          // A definitive refusal of this request: read the other decision, don't take it over.
          forget(); retry = null; uncertain = false; reload = true;
        } else {
          // A lost response is not evidence of no effect. Never manufacture another ID.
          retry = request;
          uncertain = true;
        }
      } finally {
        sending = false;
        if (reload && alive) await load();
        else render();
      }
    }
    function onClick(event) {
      const action = event.target.closest("[data-compare-action]")?.dataset.compareAction;
      if (!action) return;
      if (action === "return") { if (!sending) onReturn(); }
      if (action === "decisions") onReturn("decisions");
      if (action === "files") onFiles(comparison?.path || status?.path || options.path);
      if (action === "activity") onActivity();
      if (action === "read-current" || action === "read-saved") onRead(action === "read-current" ? "current" : "saved", comparison);
      if (action === "previous" || action === "next") diff?.navigate(action === "previous" ? -1 : 1);
      if (action === "refresh") void load();
      if (action === "check") { if (retry) void submit(retry); else void load(); }
      if (action === "confirm" && (choice === "keep_current" ? canKeep() : canUse())) void submit({
        request_id: crypto.randomUUID(), action: comparison.identical ? "keep_current" : choice,
        expected_revision: comparison.current.revision, saved_revision: comparison.saved.revision,
      });
    }
    function onChange(event) {
      if (event.target.name !== "output-choice") return;
      choice = event.target.value;
      // Update the consequence without replacing the focused radio or resetting diff position.
      root.querySelector("#compare-consequence").textContent = footerText();
    }
    function onKey(event) {
      if (event.key === "Escape" && !sending) { event.preventDefault(); onReturn(); }
      if (event.ctrlKey || event.metaKey || event.altKey || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
      if (["j", "]", "k", "["].includes(event.key)) {
        event.preventDefault();
        diff?.navigate(["j", "]"].includes(event.key) ? 1 : -1);
      }
    }
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
    root.addEventListener("keydown", onKey);
    void load();
    return { destroy() {
      alive = false;
      diff?.destroy();
      root.removeEventListener("click", onClick);
      root.removeEventListener("change", onChange);
      root.removeEventListener("keydown", onKey);
    } };
  }
  return { mount };
})();
