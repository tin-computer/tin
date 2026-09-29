// The same advisory setup readout used by MCP. Admission remains authoritative.
(() => {
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
    refresh(true);
  }
  window.TinCodeSetup = {bind};
})();
