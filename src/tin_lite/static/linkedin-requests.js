"use strict";

// Connection requests (#450, Paper INVITE-A to INVITE-C, REQ-A): the batch waiting in
// Decisions, the Sending row on the LinkedIn card, and what People says when people are ticked.
// Approving queues people; Tin sends them one at a time on its own schedule from the member's
// own session. Sending is turned on only here, with the member's own tick of the consent box.
window.TinRequests = (() => {
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // What the member has unticked in the open batch, by person; kept while they look around.
  const skipped = new Set();
  const ui = { consenting: false, consentTicked: false, editingLimits: false, saving: false };

  function day(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : `${MONTHS[date.getMonth()]} ${date.getDate()}`;
  }

  function clock(minutes) {
    return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
  }

  // "around 10:40 today", "on Friday", "on oct 26".
  function when(value, { at = false } = {}) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const now = new Date();
    const days = Math.round((new Date(date.toDateString()) - new Date(now.toDateString())) / 86400000);
    const time = `${date.getHours()}:${String(date.getMinutes()).padStart(2, "0")}`;
    if (days === 0) return at ? `around ${time} today` : "today";
    if (days === 1) return at ? `around ${time} tomorrow` : "tomorrow";
    if (days > 1 && days < 7) return `on ${DAYS[date.getDay()]}`;
    return `on ${day(date)}`;
  }

  function icpShort(text) {
    if (!text || text === "none") return "";
    const numbered = String(text).match(/^(ICP\s*\d+)\s*[:–—-]/i);
    return numbered ? numbered[1] : text;
  }

  // The window is in the project's time zone; it reads "your time" when that is the viewer's.
  function zone(sending) {
    let own = "";
    try { own = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { own = ""; }
    return sending.timezone && sending.timezone !== own ? sending.timezone : "your time";
  }

  function limitsLine(sending) {
    return `${sending.daily_cap} a day · ${sending.weekly_cap} a week · weekdays ${clock(sending.start_minute)}–${clock(sending.end_minute)} ${sending.timezone} · 15+ min apart`;
  }

  // Decisions.

  function items(data) {
    return (data?.batches || []).map((batch) => ({
      id: `requests:${batch.id}`,
      kind: "connection_requests",
      title: `Send requests to ${batch.people.length} ${batch.people.length === 1 ? "person" : "people"}`,
      created_at: batch.created_at,
      batch,
    }));
  }

  function chosen(batch) {
    return batch.people.filter((person) => !skipped.has(person.record_id));
  }

  function detail(item, data, { waiting = "" } = {}) {
    const { batch } = item;
    const sending = data?.sending || {};
    const count = chosen(batch).length;
    const rows = batch.people.map((person) => {
      const on = !skipped.has(person.record_id);
      const engaged = person.engaged ? `${day(person.engaged.at)} · ${person.engaged.kind}` : "";
      return `<label class="request-row ${on ? "" : "is-skipped"}">
        <input type="checkbox" data-request-person="${escape(person.record_id)}" ${on ? "checked" : ""} />
        <span class="request-person"><strong>${escape(person.name || "Someone")}</strong><span>${escape(person.headline || "")}</span></span>
        <span class="request-icp" title="${escape(person.icp || "")}">${escape(icpShort(person.icp))}</span>
        <code class="request-fit">${typeof person.fit === "number" ? person.fit.toFixed(2) : ""}</code>
        <code class="request-engaged">${escape(engaged)}</code>
      </label>`;
    }).join("");
    const schedule = batch.first_send && batch.last_send
      ? batch.first_send === batch.last_send
        ? ` It goes out ${when(batch.first_send, { at: true })}.`
        : ` The first goes out ${when(batch.first_send, { at: true })}, the last ${when(batch.last_send)}.`
      : "";
    const terms = `Tin sends them one at a time on its own schedule, never together: at least 15 minutes apart plus a random wait, weekdays ${clock(sending.start_minute ?? 540)}–${clock(sending.end_minute ?? 1020)} ${escape(zone(sending))}, at most ${sending.daily_cap ?? 15} a day.${sending.on ? schedule : ""} It stops at the first warning from LinkedIn.`;
    return `<article class="decision-detail-card requests-card" data-requests-batch="${escape(batch.id)}">
      <header>
        <span class="decision-workflow-mark">P</span>
        <span><strong>People who engaged with your LinkedIn posts</strong><code>${escape(waiting)}</code></span>
        <button class="open-button" type="button" data-requests-open-people>Open People</button>
      </header>
      <div class="decision-detail-body">
        <p class="decision-summary">${batch.people.length} ${batch.people.length === 1 ? "person who reacted to your posts fits" : "people who reacted to your posts fit"} your ICP and ${batch.people.length === 1 ? "isn’t" : "aren’t"} connected to you yet.</p>
        <div class="request-rows">${rows}</div>
        <p class="decision-note">${escape(terms)}</p>
        ${sending.on
          ? '<button class="requests-settings-link" type="button" data-requests-open-settings>Sending settings</button>'
          : '<p class="decision-note requests-off">Turn on sending on the LinkedIn card first. <button class="requests-settings-link" type="button" data-requests-open-settings>Open the LinkedIn card</button></p>'}
      </div>
      <footer>
        <button class="decision-discard" type="button" data-requests-action="discard">Discard</button>
        <button class="decision-approval" type="button" data-requests-action="approve" ${!sending.on || !count ? "disabled" : ""}>Approve ${count} ${count === 1 ? "request" : "requests"}</button>
      </footer>
    </article>`;
  }

  function bindDecision(root, item, ctx) {
    const card = root.querySelector("[data-requests-batch]");
    if (!card || !item) return;
    card.querySelectorAll("[data-request-person]").forEach((box) => box.addEventListener("change", () => {
      if (box.checked) skipped.delete(box.dataset.requestPerson);
      else skipped.add(box.dataset.requestPerson);
      ctx.render();
    }));
    card.querySelector("[data-requests-open-people]")?.addEventListener("click", () => ctx.navigate("people"));
    card.querySelectorAll("[data-requests-open-settings]").forEach((button) => button.addEventListener("click", () => ctx.openLinkedInCard()));
    card.querySelectorAll("[data-requests-action]").forEach((button) => button.addEventListener("click", async () => {
      const approve = button.dataset.requestsAction === "approve";
      const people = chosen(item.batch).map((person) => person.record_id);
      card.querySelectorAll("button").forEach((each) => { each.disabled = true; });
      try {
        await ctx.api(`/api/projects/${encodeURIComponent(ctx.projectId)}/linkedin-requests/batches/${encodeURIComponent(item.batch.id)}/${approve ? "approve" : "discard"}`, {
          method: "POST",
          body: JSON.stringify({ request_id: window.crypto.randomUUID(), people }),
        });
        item.batch.people.forEach((person) => skipped.delete(person.record_id));
        ctx.toast(approve
          ? `Approved. Tin sends ${people.length === 1 ? "it" : `the ${people.length}`} one at a time, starting at its next turn.`
          : "Discarded. Tin won’t propose these people again.");
        await ctx.refresh();
      } catch (error) {
        ctx.toast(`Could not record your decision: ${error.message}`);
        ctx.render();
      }
    }));
  }

  // The Sending row on the LinkedIn card.

  function toggle(on, { disabled = false, label = "Send connection requests" } = {}) {
    return `<button class="requests-switch ${on ? "is-on" : ""}" type="button" role="switch" aria-checked="${on}" aria-label="${escape(label)}" data-requests-switch ${disabled ? "disabled" : ""}><span></span></button>`;
  }

  function sendingRow(data) {
    if (!data || !data.offered) return "";
    const sending = data.sending;
    const head = (switchHtml, text, muted = false) => `<div class="requests-head">${switchHtml}<span class="${muted ? "is-muted" : ""}">${escape(text)}</span></div>`;
    let body;
    if (!data.reading) {
      const after = data.browser_only ? "turns on with cloud reading" : "turns on after Reading";
      body = head(toggle(false, { disabled: true }), `Send connection requests you approve, one at a time · ${after}`, true);
    } else if (sending.held) {
      body = `${head(toggle(true), "Connection requests you approve")}
        <p class="linkedin-part-needs" role="status">Sending paused: ${escape(data.pause_text || "LinkedIn refused a request")}. Open LinkedIn in Chrome and sort it out there, then resume. Approved requests not sent yet wait.</p>
        <div class="requests-actions"><button class="requests-resume" type="button" data-requests-resume ${ui.saving ? "disabled" : ""}>Resume sending</button><button class="requests-quiet" type="button" data-requests-off>Turn off sending</button></div>`;
    } else if (sending.on) {
      const held = sending.paused && sending.paused_until
        ? `<p class="linkedin-part-needs" role="status">LinkedIn’s weekly limit for invitations was reached. Tin waits until ${escape(when(sending.paused_until, { at: true }).replace(/^around /, ""))}.</p>` : "";
      body = `${head(toggle(true), "Connection requests you approve")}
        <span class="requests-line">${escape(limitsLine(sending))}</span>
        <span class="requests-line">stops at LinkedIn’s first warning · requests not sent within 14 days drop off</span>
        ${held}
        ${ui.editingLimits ? limitsForm(sending) : '<button class="requests-settings-link" type="button" data-requests-limits>Change limits</button>'}`;
    } else if (ui.consenting) {
      body = `${head(toggle(false), "Connection requests you approve")}
        <label class="requests-consent"><input type="checkbox" data-requests-consent ${ui.consentTicked ? "checked" : ""} /><span>${escape(data.consent.text)}</span></label>
        <p class="requests-risk">${escape(data.consent.risk)}</p>
        <div class="requests-actions"><button class="button requests-turn-on" type="button" data-requests-on ${ui.consentTicked && !ui.saving ? "" : "disabled"}>Turn on sending</button><button class="requests-quiet" type="button" data-requests-cancel>Cancel</button></div>`;
    } else {
      body = head(toggle(false), "Send connection requests you approve, one at a time");
    }
    const week = data.week || {};
    const weekRow = sending.on && data.reading
      ? `<div class="linkedin-part">
          <span class="linkedin-part-label">This week</span>
          <div class="linkedin-part-body"><span class="linkedin-part-main">${escape(weekLine(week))}</span></div>
        </div>` : "";
    return `<div class="linkedin-part requests-sending">
        <span class="linkedin-part-label">Sending</span>
        <div class="linkedin-part-body">${body}</div>
      </div>${weekRow}`;
  }

  function weekLine(week) {
    const parts = [`${week.sent || 0} sent`, `${week.accepted || 0} accepted`];
    if (week.queued) parts.push(`${week.queued} queued`);
    if (week.pending) parts.push(`${week.pending} pending`);
    if (week.next_send) parts.push(`next send ${when(week.next_send, { at: true }).replace(/^around /, "")}`);
    return parts.join(" · ");
  }

  function limitsForm(sending) {
    const hours = (selected, from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i)
      .map((hour) => `<option value="${hour * 60}" ${hour * 60 === selected ? "selected" : ""}>${hour}:00</option>`).join("");
    return `<form class="requests-limits" data-requests-limits-form>
      <label>A day <input type="number" name="daily_cap" min="1" max="25" value="${sending.daily_cap}" /></label>
      <label>A week <input type="number" name="weekly_cap" min="1" max="100" value="${sending.weekly_cap}" /></label>
      <label>From <select name="start_minute">${hours(sending.start_minute, 6, 20)}</select></label>
      <label>Until <select name="end_minute">${hours(sending.end_minute, 7, 22)}</select></label>
      <div class="requests-actions"><button class="button" type="submit" ${ui.saving ? "disabled" : ""}>Save limits</button><button class="requests-quiet" type="button" data-requests-limits-cancel>Cancel</button></div>
    </form>`;
  }

  function summary(data) {
    if (!data?.offered || !data.reading) return "";
    const sending = data.sending;
    if (sending.held) return "sending paused";
    if (!sending.on) return "";
    return `sending on · ${data.week?.sent || 0} sent this week · ${data.week?.accepted || 0} accepted`;
  }

  function needsYou(data) {
    return Boolean(data?.offered && data.reading && data.sending?.held);
  }

  function bindSending(root, ctx) {
    const row = root.querySelector(".requests-sending");
    if (!row) return;
    const base = `/api/projects/${encodeURIComponent(ctx.projectId)}/linkedin-requests`;
    const save = async (path, options, done) => {
      ui.saving = true;
      ctx.render();
      try {
        const data = await ctx.api(path, options);
        ctx.update(data);
        if (done) ctx.toast(done);
      } catch (error) {
        ctx.toast(error.message);
      } finally {
        ui.saving = false;
        ctx.render();
      }
    };
    row.querySelector("[data-requests-switch]")?.addEventListener("click", () => {
      const data = ctx.data();
      if (data?.sending?.on) {
        save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: false }) }, "Sending is off. Approved requests not sent yet wait until you turn it on.");
      } else {
        ui.consenting = !ui.consenting;
        ui.consentTicked = false;
        ctx.render();
      }
    });
    row.querySelector("[data-requests-consent]")?.addEventListener("change", (event) => {
      ui.consentTicked = event.currentTarget.checked;
      ctx.render();
    });
    row.querySelector("[data-requests-cancel]")?.addEventListener("click", () => {
      ui.consenting = false;
      ctx.render();
    });
    row.querySelector("[data-requests-on]")?.addEventListener("click", () => {
      ui.consenting = false;
      save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: true, consent_version: ctx.data().consent.version }) }, "Sending is on. Tin sends the requests you approve, one at a time.");
    });
    row.querySelector("[data-requests-off]")?.addEventListener("click", () => {
      save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: false }) }, "Sending is off.");
    });
    row.querySelector("[data-requests-resume]")?.addEventListener("click", () => {
      save(`${base}/sending/resume`, { method: "POST" }, "Sending resumed.");
    });
    row.querySelector("[data-requests-limits]")?.addEventListener("click", () => {
      ui.editingLimits = true;
      ctx.render();
    });
    row.querySelector("[data-requests-limits-cancel]")?.addEventListener("click", () => {
      ui.editingLimits = false;
      ctx.render();
    });
    row.querySelector("[data-requests-limits-form]")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const limits = Object.fromEntries(["daily_cap", "weekly_cap", "start_minute", "end_minute"].map((key) => [key, Number(form.get(key))]));
      if (limits.start_minute >= limits.end_minute) {
        ctx.toast("Choose an end after the start.");
        return;
      }
      ui.editingLimits = false;
      save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: true, consent_version: ctx.data().consent.version, limits }) }, "Limits saved.");
    });
  }

  // People (Paper REQ-A): what the bar above the grid says about sending to the people ticked.
  function picked(data) {
    const sending = data?.sending || {};
    if (!sending.on) return { ready: false, line: "Turn on sending on the LinkedIn card first." };
    if (sending.held) return { ready: true, line: `Sending is paused: ${data.pause_text || "LinkedIn refused a request"}. They wait until you resume it.` };
    const where = zone(sending);
    const window = `weekdays ${clock(sending.start_minute)}–${clock(sending.end_minute)}${where === "your time" ? "" : ` ${where}`}`;
    const first = data.next_open ? ` · the first ${when(data.next_open, { at: true })}` : "";
    return { ready: true, line: `One at a time from your LinkedIn, ${window}${first}` };
  }

  function queuedText(answer) {
    const at = answer.first_send ? when(answer.first_send, { at: true }) : "";
    if (answer.queued === 1) return `Queued. Tin sends it from your LinkedIn${at ? ` ${at}` : ""}.`;
    return `Queued. Tin sends the ${answer.queued} one at a time from your LinkedIn${at ? `, the first ${at}` : ""}.`;
  }

  // The person panel's LinkedIn row while the member's request to them waits (Paper REQ-C2).
  function waitingText(queued, data) {
    if (queued?.sending) return "Tin is sending it from your LinkedIn now.";
    const sending = data?.sending || {};
    if (!sending.on) return "It waits until you turn sending on, on the LinkedIn card. No note.";
    if (sending.held) return "It waits while sending is paused. No note.";
    const at = queued?.at ? ` ${when(queued.at, { at: true })}` : "";
    return `Tin sends it from your LinkedIn${at}, one at a time with your other requests. No note.`;
  }

  return { items, detail, bindDecision, sendingRow, summary, needsYou, bindSending, picked, queuedText, waitingText };
})();
