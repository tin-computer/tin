"use strict";

window.TinBilling = (() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const dollars = (value) => value === null || value === undefined ? "—" : `$${escape(value)}`;
  const fromNanos = (value) => value === null || value === undefined ? "—" : dollars((value / 1e9).toFixed(2));
  const safeLink = (url, host) => {
    try { const parsed = new URL(url); return parsed.protocol === "https:" && parsed.hostname === host ? parsed.href : null; }
    catch { return null; }
  };

  const accepting = new Set();
  async function paidStart({path, options, projectId, actor, fetch, assertContext}) {
    const direct = path.match(/^\/api\/workflows\/([^/]+)\/runs$/);
    const saved = path.match(/^\/api\/projects\/([^/]+)\/workflows\/([^/]+)\/(?:runs|retry)$/);
    if (!direct && !saved) return fetch(path, options);
    const body = options.body ? JSON.parse(options.body) : {};
    // A one-run card must not contribute even a hash to browser storage. The server
    // keeps the accepted run's original card immutable on duplicate starts.
    const {payment_card: _paymentCard, ...requestIdentity} = body;
    const binding = JSON.stringify([actor, projectId, path, requestIdentity]);
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(binding));
    const key = `tin-lite:paid-start:${Array.from(new Uint8Array(hash), x => x.toString(16).padStart(2,"0")).join("")}`;
    if (accepting.has(key)) throw new Error("This run request is already in progress.");
    accepting.add(key);
    try {
      // Persist only opaque identifiers, never workflow inputs. An ambiguous response
      // reuses the same request even after a page reload. Older pending requests
      // may also carry a quote ID; retain that binding for compatibility.
      let accepted = JSON.parse(sessionStorage.getItem(key) || "null");
      if (!accepted) {
        assertContext();
        accepted = {requestId: options.headers?.["Idempotency-Key"] || `ui:${crypto.randomUUID()}`};
        sessionStorage.setItem(key, JSON.stringify(accepted));
      }
      assertContext();
      const next = {...options, headers:{...options.headers, "Idempotency-Key":accepted.requestId}};
      if (accepted.quoteId) {
        if (direct) next.body = JSON.stringify({...body, billing_quote_id:accepted.quoteId});
        else next.headers["Tin-Billing-Quote"] = accepted.quoteId;
      }
      try {
        const response = await fetch(path, next);
        const receipt = await response.clone().json();
        if (typeof receipt.id !== "string") throw new Error("Run confirmation is incomplete. Retry this request.");
        sessionStorage.removeItem(key);
        return response;
      } catch (error) {
        // A 5xx or transport failure can follow a committed run. Never repurchase it.
        if (error.status >= 400 && error.status < 500) sessionStorage.removeItem(key);
        throw error;
      }
    } finally { accepting.delete(key); }
  }

  function bindEstimate(form, {projectId, fetch, request, isCurrent}) {
    const container = form.querySelector(".system-config-when, .workflow-config-rows") || form;
    const label = document.createElement("p");
    label.className = "system-config-note";
    label.dataset.workflowCost = "";
    label.hidden = true;
    container.append(label);
    let lastRequest = null, sequence = 0, timer;
    async function update() {
      if (!form.isConnected || !isCurrent()) return;
      let payload;
      try { payload = request(); } catch { payload = null; }
      if (!payload) { ++sequence; lastRequest = null; label.hidden = true; return; }
      const binding = JSON.stringify(payload);
      if (binding === lastRequest) return;
      const current = ++sequence;
      lastRequest = binding;
      label.hidden = true;
      try {
        const response = await fetch(`/api/projects/${projectId}/billing/estimate`, {
          method: "POST", body: binding,
        });
        const cost = await response.json();
        if (current !== sequence || !form.isConnected || !isCurrent()) return;
        if (cost.estimated_usd == null) return;
        label.textContent = cost.estimated_usd === "0.00" ? "No workflow charge" :
          cost.estimate?.basis === "conservative_configured_bound" ?
          `Maximum charge: $${cost.maximum_usd} per run · actual usage is charged` :
          `${payload.project_workflow_id ? "Saved estimate" : "Estimated cost"}: up to $${cost.estimated_usd} per run · actual usage is charged`;
        label.title = cost.notice || "";
        label.hidden = false;
      } catch {
        // Incomplete inputs are normal while configuring. Starting still runs the
        // authoritative server check; never display a fabricated free estimate.
        if (current === sequence) lastRequest = null;
      }
    }
    form.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(update, 300); });
    form.addEventListener("change", () => { clearTimeout(timer); timer = setTimeout(update, 0); });
    update();
  }

  function mount(main, { api, projectId, projectName, workspaceName, onChange }) {
    const root = document.createElement("section");
    root.className = "product-view workspace-view billing-view";
    root.innerHTML = '<div class="view-loading">Loading billing…</div>';
    main.replaceChildren(root);
    const path = `/api/projects/${encodeURIComponent(projectId)}/billing`;
    let lastData = null;

    async function load() {
      try {
        const data = await api(path);
        if (!root.isConnected) return;
        lastData = data;
        onChange?.(data);
        if (!data.enabled) {
          root.innerHTML = '<header class="workspace-header"><h1>Billing</h1></header><p>Billing is not enabled for this workspace.</p>';
          return;
        }
        const payments = data.is_admin ? await api(`/api/workspaces/${data.workspace_id}/billing/payments`) : [];
        if (!root.isConnected) return;
        render(data, payments);
      } catch (error) {
        if (!root.isConnected) return;
        root.innerHTML = `<header class="workspace-header"><h1>Billing</h1></header><p role="alert">Couldn’t load billing.</p><button data-retry>Retry</button>`;
        root.querySelector("[data-retry]").addEventListener("click", load);
      }
    }

    function render(data, payments) {
      const invoices = payments.filter((p) => safeLink(p.invoice_url, "invoice.stripe.com"));
      root.innerHTML = `<header class="workspace-header billing-heading"><h1>${data.is_admin ? "Billing" : "Spending"}</h1>
        <span>${escape(workspaceName)}${data.mode === "test" ? " · Test mode" : ""}</span></header>
        <div class="billing-balance"><div class="billing-balance-facts"><span class="billing-label">${data.is_admin ? "Available" : "Spent this month"}</span>
          <div class="billing-amount">${dollars(data.is_admin ? data.available_usd : data.spent_this_month_usd)}</div>
          ${data.is_admin ? "" : `<p>${escape(projectName)}</p>`}
          ${data.status === "suspended" ? '<p role="status">New paid runs paused</p>' : ""}
          ${data.run_billing_enabled === false ? '<p>Welcome credits are saved. Workflow charging is not enabled for this workspace yet.</p>' : ""}
        </div>${data.is_admin ? `<form class="billing-topup"><label class="billing-label" for="billing-amount">Add funds</label>
          <div class="billing-presets">${[10,25,100].map((n) => `<button type="button" data-amount="${n}">$${n}</button>`).join("")}</div>
          <input id="billing-amount" name="amount" type="number" min="${data.topup_min_cents / 100}" max="${data.topup_max_cents / 100}" step="0.01" value="10" required aria-describedby="billing-topup-help">
          <button class="billing-primary" type="submit">Add $10.00</button>
          <small id="billing-topup-help">Checkout with Stripe${data.mode === "test" ? " · Test mode" : ""}</small><p class="billing-error" role="alert"></p></form>` : ""}</div>
        ${data.is_admin ? `<div class="billing-account"><div class="billing-account-row"><strong>Invoices</strong><span>${invoices.length ? `${invoices.length} available` : "None yet"}</span>
          ${invoices.length ? '<button data-invoices>View invoices →</button>' : ""}</div><div class="billing-account-row"><strong>Billing admin</strong><span>You</span></div></div>` : ""}
        <section><h2>Project limits</h2><div class="billing-table"><div class="billing-policy-head"><span>Project</span><span>Monthly limit</span><span>Per run</span><span></span></div>
          ${data.policies.map((p) => `<div class="billing-policy-row"><strong>${escape(p.name)}</strong><span>${fromNanos(p.monthly_nanos)}</span><span>${fromNanos(p.per_run_nanos)}</span>
            ${data.is_admin ? `<button data-limits="${escape(p.id)}">${p.revision ? "Edit" : "Set limits"} →</button>` : "<span></span>"}</div>`).join("")}</div><div data-policy-editor></div></section>
        <section><div class="billing-section-heading"><h2>Transactions</h2><button data-refresh>Refresh</button></div>
          ${!data.transactions.length ? '<p class="billing-empty">No transactions yet.</p>' : `<div class="billing-transactions">
            ${data.transactions.map((t) => `<div class="billing-transaction"><time>${escape(new Date(t.created_at).toLocaleDateString())}</time><span>${escape(({topup:"Funds added",welcome_credit:"Welcome credits",charge:"Workflow run",refund:"Refund",dispute:"Payment disputed",adjustment:"Adjustment"})[t.kind] || t.kind)}</span><span>${dollars(t.amount_usd)}</span></div>`).join("")}</div>`}
        </section><section id="billing-invoices"><h2>Top-ups and invoices</h2>${payments.filter((p) => p.status === "paid").map((p) => {
          const url = safeLink(p.invoice_url, "invoice.stripe.com");
          return `<div class="billing-account-row"><strong>${dollars(p.amount_usd)} added</strong><span>${url ? `<a href="${escape(url)}" target="_blank" rel="noopener">View invoice →</a>` : "Invoice pending"}
            ${Number(p.refund_pending_usd) > 0 ? `<small> · ${dollars(p.refund_pending_usd)} refund pending</small>` : ""}</span></div>`;
        }).join("") || '<p class="billing-muted">None yet.</p>'}</section>`;
      if (!data.is_admin) root.querySelector("#billing-invoices").remove();
      root.querySelector("[data-refresh]").addEventListener("click", load);
      root.querySelector("[data-invoices]")?.addEventListener("click", () => root.querySelector("#billing-invoices").scrollIntoView({behavior:"smooth"}));
      const form = root.querySelector(".billing-topup");
      if (form) {
        const input = form.elements.amount;
        const update = () => { form.querySelector('[type="submit"]').textContent = `Add $${Number(input.value || 0).toFixed(2)}`; delete form.dataset.requestId; };
        input.addEventListener("input", update);
        form.querySelectorAll("[data-amount]").forEach((b) => b.addEventListener("click", () => {input.value=b.dataset.amount; update();}));
        form.addEventListener("submit", async (event) => {
          event.preventDefault();
          if (!form.reportValidity()) return;
          const button = form.querySelector('[type="submit"]');
          button.disabled = true;
          form.dataset.requestId ||= crypto.randomUUID();
          try {
            const payment = await api(`/api/workspaces/${data.workspace_id}/billing/checkout`, {method:"POST", body:JSON.stringify({amount_cents:Math.round(Number(input.value)*100),request_id:form.dataset.requestId})});
            if (!root.isConnected) return;
            const url = safeLink(payment.checkout_url, "checkout.stripe.com");
            if (url) window.location.assign(url); else await load();
          } catch (error) { form.querySelector('[role="alert"]').textContent=error.message; button.disabled=false; }
        });
      }
      root.querySelectorAll("[data-limits]").forEach((b) => b.addEventListener("click", () => editPolicy(data.policies.find((p) => p.id===b.dataset.limits))));
    }

    function editPolicy(policy) {
      const editor = root.querySelector("[data-policy-editor]");
      editor.innerHTML = `<form class="billing-editor"><h3>${escape(policy.name)}</h3><label>Monthly limit (USD)<input name="monthly" type="number" min="0.01" step="0.01" value="${policy.monthly_nanos ? policy.monthly_nanos/1e9 : ""}" required></label>
        <label>Per run (USD)<input name="perRun" type="number" min="0.01" step="0.01" value="${policy.per_run_nanos ? policy.per_run_nanos/1e9 : ""}" required></label>
        <label>Per scheduled run (USD)<input name="scheduledRun" type="number" min="0.01" step="0.01" value="${policy.schedule_max_nanos ? policy.schedule_max_nanos/1e9 : ""}" placeholder="Not enabled" aria-describedby="billing-schedule-help"></label>
        <small id="billing-schedule-help">Leave blank to keep paid scheduled runs off. Each run also stays within your other limits.</small>
        <footer><button type="button" data-cancel>Cancel</button><button type="submit" class="billing-primary">Save limits</button></footer><p role="alert"></p></form>`;
      const form = editor.querySelector("form");
      form.elements.monthly.focus();
      form.querySelector("[data-cancel]").addEventListener("click", () => editor.replaceChildren());
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]'); button.disabled=true;
        try {
          await api(`/api/projects/${policy.id}/billing/limits`, {method:"PUT",body:JSON.stringify({monthly_nanos:Math.round(Number(form.elements.monthly.value)*1e9),per_run_nanos:Math.round(Number(form.elements.perRun.value)*1e9),expected_revision:policy.revision || 0,schedule_max_nanos:form.elements.scheduledRun.value ? Math.round(Number(form.elements.scheduledRun.value)*1e9) : null})});
          if (root.isConnected) await load();
        } catch(error) { form.querySelector('[role="alert"]').textContent=error.message; button.disabled=false; }
      });
    }

    load();
    return { refresh: load, get data() { return lastData; } };
  }
  return {mount, paidStart, bindEstimate};
})();
