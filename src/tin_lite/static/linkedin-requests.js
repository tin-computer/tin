"use strict";

// Connection requests (#450, Paper INVITE-A to INVITE-C, REQ-A): the Sending row on the LinkedIn
// card, and what People says when people are ticked. Tin queues the people a LinkedIn read finds
// who fit, and those the member chooses, and sends them one at a time on its own schedule from
// the member's own session. Sending is on by default once reading runs in the cloud; off stays
// off.
window.TinRequests = (() => {
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ui = { consenting: false, consentTicked: true, editingLimits: false, saving: false };

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

  // The window is in the project's time zone; it reads "your time" when that is the viewer's.
  function zone(sending) {
    let own = "";
    try { own = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { own = ""; }
    return sending.timezone && sending.timezone !== own ? sending.timezone : "your time";
  }

  function limitsLine(sending) {
    return `${sending.daily_cap} a day · ${sending.weekly_cap} a week · weekdays ${clock(sending.start_minute)}–${clock(sending.end_minute)} ${sending.timezone} · 15+ min apart`;
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
      body = head(toggle(false, { disabled: true }), `Send connection requests, one at a time · ${after}`, true);
    } else if (sending.held) {
      body = `${head(toggle(true), "Connection requests")}
        <p class="linkedin-part-needs" role="status">Sending paused: ${escape(data.pause_text || "LinkedIn refused a request")}. Open LinkedIn in Chrome and sort it out there, then resume. Requests not sent yet wait.</p>
        <div class="requests-actions"><button class="requests-resume" type="button" data-requests-resume ${ui.saving ? "disabled" : ""}>Resume sending</button><button class="requests-quiet" type="button" data-requests-off>Turn off sending</button></div>`;
    } else if (sending.on) {
      const held = sending.paused && sending.paused_until
        ? `<p class="linkedin-part-needs" role="status">LinkedIn’s weekly limit for invitations was reached. Tin waits until ${escape(when(sending.paused_until, { at: true }).replace(/^around /, ""))}.</p>` : "";
      body = `${head(toggle(true), "Connection requests")}
        <span class="requests-line">${escape(limitsLine(sending))}</span>
        <span class="requests-line">stops at LinkedIn’s first warning</span>
        ${held}
        ${ui.editingLimits ? limitsForm(sending) : '<button class="requests-settings-link" type="button" data-requests-limits>Change limits</button>'}`;
    } else if (ui.consenting) {
      body = `${head(toggle(false), "Connection requests")}
        <label class="requests-consent"><input type="checkbox" data-requests-consent ${ui.consentTicked ? "checked" : ""} /><span>${escape(data.consent.text)}</span></label>
        <div class="requests-actions"><button class="button requests-turn-on" type="button" data-requests-on ${ui.consentTicked && !ui.saving ? "" : "disabled"}>Turn on sending</button><button class="requests-quiet" type="button" data-requests-cancel>Cancel</button></div>`;
    } else {
      body = head(toggle(false), "Send connection requests, one at a time");
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
        save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: false }) }, "Sending is off. Requests not sent yet wait until you turn it on.");
      } else {
        ui.consenting = !ui.consenting;
        // Members agreed to sending when they joined Tin; the box starts ticked.
        ui.consentTicked = true;
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
      save(`${base}/sending`, { method: "PUT", body: JSON.stringify({ enabled: true, consent_version: ctx.data().consent.version }) }, "Sending is on. Tin sends your connection requests one at a time.");
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

  // Sending is on by default with cloud reading: off means the member turned it off, or reads
  // in this browser only, with no saved session to send with.
  function offLine(data) {
    return data?.reading ? "Turn on sending on the LinkedIn card first." : "Turn on cloud reading on the LinkedIn card first.";
  }

  // People (Paper REQ-A): what the bar above the grid says about sending to the people ticked.
  function picked(data) {
    const sending = data?.sending || {};
    if (!sending.on) return { ready: false, line: offLine(data) };
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

  return { sendingRow, summary, needsYou, bindSending, picked, queuedText, waitingText };
})();
