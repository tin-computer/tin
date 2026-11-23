// RB2B's card on Integrations: the project's own webhook address, to paste into RB2B, and what
// arrived since. app.js lists the card; this file draws it and handles its buttons.
(() => {
  const KEY = "visitors.rb2b";
  const LOGO = '<svg aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="5.5" r="2.5" /><path d="M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4" /></svg>';
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const ui = { view: null, projectId: null, loading: false, busy: false, confirm: null, error: null };

  function when(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return "";
    const minutes = String(date.getMinutes()).padStart(2, "0");
    return `${MONTHS[date.getMonth()]} ${date.getDate()}, ${date.getHours()}:${minutes}`;
  }

  // What the card says on its row: whether RB2B has posted, and when.
  function health(view) {
    if (!view?.set_up) return null;
    if (view.start_error) return ["needs you", view.start_error];
    if (view.last_received_at) return ["receiving", `last visitor ${when(view.last_received_at)}`];
    return ["waiting for RB2B", "paste the address into RB2B"];
  }

  // The card as an entry in the Integrations list, so search and filters see it.
  function entry() {
    const view = ui.view;
    return {
      key: KEY,
      name: "RB2B",
      description: "Website visitors RB2B identifies, kept in People and rated against your ICP.",
      unlocks: ["visitors in People", "rated against your ICP"],
      connection_id: view?.set_up ? "rb2b" : null,
      status: view?.start_error ? "needs_attention" : view?.set_up ? "connected" : "available",
    };
  }

  function card(expanded) {
    const view = ui.view;
    const busy = ui.busy ? " disabled" : "";
    const said = health(view);
    const action = view?.set_up
      ? `<button class="integration-row-action" type="button" data-integration-expand="${KEY}" aria-expanded="${expanded}">Configure</button>`
      : `<button class="integration-connect" type="button" data-rb2b-setup${busy}>${ui.loading ? "Loading…" : "Set up"}</button>`;
    return `<article class="integration-card rb2b-card ${view?.set_up ? "is-connected" : "is-available"} ${expanded ? "is-expanded" : ""}">
      <div class="integration-card-row">
        <span class="integration-badge" aria-hidden="true">${LOGO}</span>
        <span class="integration-identity">
          <strong class="integration-name">RB2B</strong>
          <code class="integration-key">${KEY}</code>
        </span>
        <span class="integration-state-mark is-${view?.start_error ? "needs_attention" : view?.set_up ? "connected" : "available"}" aria-hidden="true"></span>
        ${said
          ? `<span class="integration-primary">${escape(said[0])}</span><span class="integration-health">${escape(said[1])}</span>`
          : `<span class="integration-unlocks">would unlock visitors in People · rated against your ICP</span>`}
        ${action}
      </div>
      ${ui.error ? `<div class="analytics-prompt" role="status">${escape(ui.error)}</div>` : ""}
      ${expanded && view?.set_up ? details(view) : ""}
    </article>`;
  }

  function part(label, main, extra = "") {
    return `<div class="linkedin-part">
      <span class="linkedin-part-label">${escape(label)}</span>
      <div class="linkedin-part-body">${main}${extra}</div>
    </div>`;
  }

  function details(view) {
    const busy = ui.busy ? " disabled" : "";
    const counts = [
      `${view.people} ${view.people === 1 ? "person" : "people"}`,
      `${view.companies} ${view.companies === 1 ? "company" : "companies"} only`,
      view.waiting ? `${view.waiting} waiting to be read` : "",
    ].filter(Boolean).join(" · ");
    const confirm = ui.confirm === "rotate"
      ? `<p class="linkedin-part-needs" role="status">The address in RB2B stops working until you paste the new one there.</p>
         <button class="integration-row-action" type="button" data-rb2b-rotate${busy}>Make a new address</button>
         <button class="integration-row-action is-quiet" type="button" data-rb2b-cancel>Keep this one</button>`
      : ui.confirm === "remove"
        ? `<p class="linkedin-part-needs" role="status">RB2B's posts stop reaching Tin. People and ratings already read stay.</p>
           <button class="integration-row-action" type="button" data-rb2b-remove${busy}>Remove</button>
           <button class="integration-row-action is-quiet" type="button" data-rb2b-cancel>Keep it</button>`
        : `<button class="integration-row-action" type="button" data-rb2b-confirm="rotate"${busy}>New address</button>
           <button class="integration-row-action is-quiet" type="button" data-rb2b-confirm="remove"${busy}>Remove</button>`;
    return `<div class="linkedin-parts rb2b-parts">
      ${part("Address", `<code class="rb2b-url">${escape(view.url)}</code>`, `<button class="integration-row-action" type="button" data-rb2b-copy>Copy</button>
        <p class="linkedin-part-meta">In RB2B, open Integrations, choose Webhook and paste this address. Turn on “Send repeat visitor data” to read every visit, then send a test event.</p>`)}
      ${part("Received", `<span class="linkedin-part-main">${escape(view.last_received_at ? `last ${when(view.last_received_at)}` : "nothing yet")}</span>`,
        `${view.last_test_at ? `<code class="linkedin-part-meta">test event received ${escape(when(view.last_test_at))}</code>` : ""}<code class="linkedin-part-meta">${escape(counts)}</code>${view.start_error ? `<p class="linkedin-part-needs" role="status">${escape(view.start_error)}</p>` : ""}`)}
      ${part("Runs", `<span class="linkedin-part-main">${escape(view.set_up_by_you ? "start as you" : "start as the teammate who set it up")}</span>`)}
      <div class="linkedin-part rb2b-actions">${confirm}</div>
    </div>`;
  }

  async function load(api, projectId, rerender) {
    if (ui.loading || (ui.projectId === projectId && ui.view)) return;
    ui.loading = true;
    ui.projectId = projectId;
    try {
      const view = await api(`/api/projects/${encodeURIComponent(projectId)}/rb2b`);
      if (ui.projectId === projectId) ui.view = view;
    } catch (error) {
      if (ui.projectId === projectId) ui.view = { set_up: false, unavailable: error.message };
    } finally {
      ui.loading = false;
    }
    rerender();
  }

  async function change(api, projectId, rerender, method) {
    ui.busy = true;
    ui.error = null;
    rerender();
    try {
      const path = `/api/projects/${encodeURIComponent(projectId)}/rb2b`;
      if (method === "DELETE") {
        await api(path, { method });
        ui.view = { set_up: false };
      } else {
        ui.view = await api(path, { method: "POST" });
      }
      ui.confirm = null;
    } catch (error) {
      ui.error = error.message;
    } finally {
      ui.busy = false;
    }
    rerender();
  }

  function bind(root, { api, projectId, rerender }) {
    root.querySelector("[data-rb2b-setup]")?.addEventListener("click", () => change(api, projectId, rerender, "POST"));
    root.querySelector("[data-rb2b-rotate]")?.addEventListener("click", () => change(api, projectId, rerender, "POST"));
    root.querySelector("[data-rb2b-remove]")?.addEventListener("click", () => change(api, projectId, rerender, "DELETE"));
    root.querySelectorAll("[data-rb2b-confirm]").forEach((button) => button.addEventListener("click", () => {
      ui.confirm = button.dataset.rb2bConfirm;
      rerender();
    }));
    root.querySelector("[data-rb2b-cancel]")?.addEventListener("click", () => {
      ui.confirm = null;
      rerender();
    });
    root.querySelector("[data-rb2b-copy]")?.addEventListener("click", async (event) => {
      try {
        await navigator.clipboard.writeText(ui.view?.url || "");
        event.target.textContent = "Copied";
      } catch {
        event.target.textContent = "Select and copy";
      }
    });
  }

  function forProject(projectId) {
    if (ui.projectId !== projectId) {
      ui.projectId = null;
      ui.view = null;
      ui.confirm = null;
      ui.error = null;
    }
  }

  window.TinRB2B = { KEY, entry, card, bind, load, forProject };
})();
