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
    let generation = 0, timer, lastData, lastInputs;
    function paragraph(text, kind = "") {
      const row = document.createElement("p"); row.textContent = text;
      if (kind) row.className = kind;
      panel.append(row);
    }
    function render(data) {
      panel.replaceChildren();
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
      for (const provider of estimate.external_providers || []) {
        const label = `${provider.provider_name} API`;
        if (provider.status === "free") {
          paragraph(`${label} · Free`, "code-setup-provider");
        } else if (provider.status === "creator_estimate") {
          const details = document.createElement("details");
          const summary = document.createElement("summary");
          summary.textContent = `${label} · About $${provider.estimated_usd}/run, billed separately`;
          const explanation = document.createElement("p");
          explanation.textContent = `Creator estimate: ${provider.basis}`;
          details.append(summary, explanation);
          if (typeof provider.pricing_url === "string" && provider.pricing_url.startsWith("https://")) {
            const link = document.createElement("a"); link.href = provider.pricing_url;
            link.textContent = "Provider pricing ↗"; link.target = "_blank"; link.rel = "noopener noreferrer";
            details.append(link);
          }
          panel.append(details);
        } else {
          paragraph(`${label} · Billed by provider; estimate unavailable`, "code-setup-provider");
        }
      }
      if (!estimate.external_providers && estimate.external_provider_cost === "unknown") paragraph("External API · Billed separately", "code-setup-provider");
      for (const connection of data.connections) {
        const provider = estimate.external_providers?.find((item) => item.provider_key === connection.provider_key);
        paragraph(`${provider?.provider_name || connection.provider_key}: ${connection.ready ? "connected" : "needs setup"}`);
      }
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
    }
    async function refresh(initial = false) {
      const requestGeneration = ++generation;
      if (!services.isCurrent() || !form.isConnected) return;
      const body = configured ? {project_workflow_id: configured.id} : {workflow_id: workflow.id};
      if (!initial || !configured) body.inputs = services.readInputs();
      const inputsKey = JSON.stringify(body.inputs || configured?.inputs || {});
      if (lastData && inputsKey === lastInputs) return;
      try {
        const response = await services.fetch(`/api/projects/${services.projectId}/workflow-setup`, {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
        });
        const data = await response.json();
        if (requestGeneration !== generation || !services.isCurrent() || !form.isConnected) return;
        if (!response.ok) {
          lastData = null;
          panel.replaceChildren(); paragraph("Complete valid inputs to check workflow setup.");
          return;
        }
        lastData = data; lastInputs = inputsKey;
        render(data);
      } catch {
        if (requestGeneration === generation && services.isCurrent() && form.isConnected) {
          lastData = null;
          panel.replaceChildren(); paragraph("Setup check unavailable. Try reopening this workflow.");
        }
      }
    }
    function changed(event) {
      if (event.target.name === "schedule_mode") {
        if (lastData) render(lastData);
        return;
      }
      if (!event.target.name?.startsWith("input:")) return;
      ++generation; clearTimeout(timer); timer = setTimeout(() => refresh(), 250);
    }
    form.addEventListener("input", changed);
    form.addEventListener("change", changed);
    paragraph("Checking workflow setup…");
    refresh(true);
  }
  window.TinCodeSetup = {bind};
})();
