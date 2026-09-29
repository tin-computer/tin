// The same advisory setup readout used by MCP. Admission remains authoritative.
(() => {
  async function bindSources(form, services) {
    if (form.dataset.codeSourcesBound) return;
    form.dataset.codeSourcesBound = "true";
    const {workflow, configured} = services;
    const url = `/api/projects/${encodeURIComponent(services.projectId)}/workflow-sources/${encodeURIComponent(workflow.id)}`
      + (configured ? `?project_workflow_id=${encodeURIComponent(configured.id)}` : "");
    const buttons = [...form.querySelectorAll("button[type=submit]")];
    for (const button of buttons) button.disabled = true;
    let slots;
    try {
      const response = await services.fetch(url);
      if (!response.ok) {
        const problem = await response.json().catch(() => null);
        throw new Error(response.status === 409 && typeof problem?.detail === "string"
          ? problem.detail : "Approved source choices are unavailable.");
      }
      slots = (await response.json()).slots || [];
    } catch (error) {
      if (services.isCurrent() && form.isConnected) {
        delete form.dataset.codeSourcesBound;
        const message = form.querySelector("[data-source-error]") || document.createElement("p");
        message.className = "code-setup-issue";
        message.dataset.sourceError = "true";
        message.replaceChildren();
        message.append(`${error.message || "Approved source choices are unavailable."} `);
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "system-action";
        retry.textContent = "Retry source choices";
        retry.addEventListener("click", () => bindSources(form, services));
        message.append(retry);
        if (!message.isConnected) {
          const footer = form.querySelector(".system-config-footer, .workflow-config-actions");
          if (footer) footer.before(message); else form.append(message);
        }
      }
      return false;
    }
    if (!services.isCurrent() || !form.isConnected) return;
    form.querySelector("[data-source-error]")?.remove();
    for (const slot of slots) {
      if (form.dataset.workflowField && form.dataset.workflowField !== `input:${slot.input}`) continue;
      const control = form.elements.namedItem(form.dataset.workflowField ? "value" : `input:${slot.input}`);
      if (!control || control.tagName !== "INPUT") continue;
      const selected = control.value;
      const select = document.createElement("select");
      select.className = "workflow-inline-input approved-source-select";
      select.name = control.name;
      select.id = control.id;
      select.required = Boolean(slot.required);
      select.dataset.approvedSourcePicker = slot.slot;
      select.add(new Option(slot.candidates.length ? "Choose an approved source…" : "No approved sources available", ""));
      for (const candidate of slot.candidates) {
        const date = candidate.created_at ? new Date(candidate.created_at).toLocaleDateString() : "";
        const producer = String(candidate.producer_title || candidate.workflow_key || slot.workflow_key || "Reviewed result").replace(/[._]/g, " ");
        select.add(new Option(
          [candidate.title || "Saved result", producer, date].filter(Boolean).join(" · "),
          candidate.run_id,
        ));
      }
      select.value = slot.candidates.some(candidate => candidate.run_id === selected) ? selected : "";
      const details = document.createElement("small");
      details.className = "workflow-field-message approved-source-details";
      details.setAttribute("aria-live", "polite");
      const update = () => {
        details.replaceChildren();
        const candidate = slot.candidates.find(item => item.run_id === select.value);
        if (!candidate) {
          details.textContent = selected && !slot.candidates.some(item => item.run_id === selected)
            ? "Previously selected source is no longer eligible. Choose another reviewed result."
            : slot.missing_reason || "Choose a reviewed result from this project.";
          return;
        }
        const description = document.createTextNode("Approved saved copy · ");
        const link = document.createElement("a");
        link.href = candidate.read_url;
        link.title = `Saved revision ${candidate.revision || ""}`;
        link.textContent = "Read reviewed copy →";
        details.append(description, link);
      };
      select.addEventListener("change", update);
      control.replaceWith(select);
      select.after(details);
      update();
    }
    for (const button of buttons) button.disabled = false;
    form.dispatchEvent(new Event("change", {bubbles: true}));
    return true;
  }

  function bind(form, services) {
    if (form.dataset.codeSetupBound) return;
    form.dataset.codeSetupBound = "true";
    const {workflow, configured} = services;
    form.tinCodeSchedule = configured?.schedule || {};
    const panel = document.createElement("section");
    panel.className = "code-workflow-setup";
    panel.setAttribute("aria-label", "Workflow setup");
    panel.setAttribute("aria-live", "polite");
    const footer = form.querySelector(".system-config-footer, .workflow-config-actions");
    (footer || form.lastElementChild).before(panel);
    const weekday = form.querySelector(".schedule-weekday .workflow-row-control");
    if (weekday) {
      const selected = configured?.schedule?.weekdays || ["tuesday"];
      const days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
      weekday.replaceChildren();
      weekday.dataset.codeWeekdays = "true";
      for (const day of days) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox"; input.name = "schedule_days"; input.value = day;
        input.checked = selected.includes(day);
        label.append(input, day[0].toUpperCase() + day.slice(1));
        weekday.append(label);
      }
    }
    let generation = 0, timer;
    function paragraph(text, kind = "") {
      const row = document.createElement("p"); row.textContent = text;
      if (kind) row.className = kind;
      panel.append(row);
    }
    async function refresh(initial = false) {
      const requestGeneration = ++generation;
      if (!services.isCurrent() || !form.isConnected) return;
      const body = configured ? {project_workflow_id: configured.id} : {workflow_id: workflow.id};
      if (!initial || !configured) body.inputs = services.readInputs();
      try {
        const response = await services.fetch(`/api/projects/${services.projectId}/workflow-setup`, {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
        });
        const data = await response.json();
        if (requestGeneration !== generation || !services.isCurrent() || !form.isConnected) return;
        panel.replaceChildren();
        if (!response.ok) {
          paragraph("Complete valid inputs to check workflow setup.");
          return;
        }
        const modes = new Set(data.schedule_modes);
        for (const button of form.querySelectorAll("[data-tin-segment]")) {
          if (button.closest(".tin-segmented")?.querySelector("[name=schedule_mode]")) {
            button.hidden = !modes.has(button.dataset.tinSegment === "manual" ? "on_demand" : button.dataset.tinSegment);
          }
        }
        const estimate = data.estimate;
        paragraph(estimate.basis === "included_bounded_compute"
          ? "Included compute · 0 Tin credits"
          : `Estimated Tin model cost: up to $${estimate.estimated_usd} per run`);
        if (estimate.external_provider_cost === "unknown") paragraph("External API costs are billed by your provider; Tin cannot estimate them.");
        for (const connection of data.connections) paragraph(`${connection.provider_key}: ${connection.ready ? "connected" : "needs setup"}`);
        const scheduled = form.elements.schedule_mode?.value !== "manual";
        const issues = [...data.issues, ...(scheduled ? data.schedule_issues : [])];
        for (const picker of form.querySelectorAll("[data-approved-source-picker]")) {
          if (picker.required && !picker.value) issues.push("Choose an approved project result before starting this workflow.");
        }
        for (const issue of new Set(issues)) paragraph(issue, "code-setup-issue");
        if (!issues.length) paragraph(scheduled ? "Setup ready for scheduling." : "Setup ready to run.");
        if (data.connections.length) {
          const link = document.createElement("a"); link.href = `/integrations?project=${encodeURIComponent(services.projectId)}`;
          link.textContent = "Manage connections →"; panel.append(link);
        }
        if (configured?.last_error) paragraph(configured.last_error, "code-setup-issue");
        for (const [key, label] of [["start_at", "Starts"], ["end_at", "Ends"]]) {
          if (configured?.schedule?.[key]) paragraph(`${label} ${new Date(configured.schedule[key]).toLocaleString()}.`);
        }
      } catch {
        if (requestGeneration === generation && services.isCurrent() && form.isConnected) {
          panel.replaceChildren(); paragraph("Setup check unavailable. Try reopening this workflow.");
        }
      }
    }
    function changed() { ++generation; clearTimeout(timer); timer = setTimeout(() => refresh(), 250); }
    form.addEventListener("input", changed);
    form.addEventListener("change", changed);
    paragraph("Checking workflow setup…");
    bindSources(form, services).then(ready => {
      if (ready) refresh(true);
      else if (services.isCurrent() && form.isConnected) {
        panel.replaceChildren();
        paragraph("Source choices unavailable. Retry source choices to check setup.", "code-setup-issue");
      }
    });
  }
  window.TinCodeSetup = {bind, bindSources};
})();
