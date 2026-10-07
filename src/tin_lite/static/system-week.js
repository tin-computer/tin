"use strict";

// The System calendar: one week of what ran and what is coming, from GET /api/projects/{id}/week.
// Paper: "System · this week study", boards WK-N6 (the week) and WK-N5b (a busy day opened).
// A day shows at most three entries. What needs the founder, what failed or runs now, and
// anything from today on is a card; finished history is one line. The rest, stopped runs
// included, opens over the calendar from "+N more", so a busy day never pushes the page down.
// The opened day sits inside its own column: wide, it lifts over its neighbours; narrow, where
// the days stack, it takes the column's place.
(() => {
  const VISIBLE = 3;
  // Lower ranks are shown first, so a review is never the entry hidden behind "+N more".
  const RANK = { for_you: 0, failed: 1, running: 2, held: 3, planned: 3, paused: 4, skipped: 4, done: 5, stopped: 6 };
  const LABEL = {
    for_you: "for you", failed: "failed", running: "running", done: "done", stopped: "stopped",
    planned: "planned", held: "held", paused: "paused", skipped: "skipped",
  };

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  // A calendar date (YYYY-MM-DD) is a wall-clock day; arithmetic on it stays in UTC so a
  // clock change never moves it.
  const isoDay = (date) => date.toISOString().slice(0, 10);
  const parseDay = (iso) => new Date(`${iso}T00:00:00Z`);
  function addDays(iso, days) {
    const date = parseDay(iso);
    date.setUTCDate(date.getUTCDate() + days);
    return isoDay(date);
  }

  function localDay(instant, timeZone) {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" })
      .formatToParts(new Date(instant));
    const part = (type) => parts.find((item) => item.type === type).value;
    return `${part("year")}-${part("month")}-${part("day")}`;
  }

  // The Monday of the week holding `instant`, in the viewer's time zone.
  function weekStart(instant, timeZone) {
    const today = localDay(instant, timeZone);
    const weekday = (parseDay(today).getUTCDay() + 6) % 7;
    return addDays(today, -weekday);
  }

  const clock = (instant, timeZone) => new Date(instant).toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone,
  });
  // "mon 5": built from parts, since engines order a weekday and a day differently.
  const dayLabel = (iso) => {
    const date = parseDay(iso);
    const weekday = date.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" }).toLowerCase();
    return `${weekday} ${date.getUTCDate()}`;
  };
  const dayName = (iso) => parseDay(iso).toLocaleDateString("en-US", {
    weekday: "long", day: "numeric", month: "long", timeZone: "UTC",
  });

  function runState(run, needsYou) {
    if (run.status === "needs_input" || needsYou.has(run.id)) return "for_you";
    if (run.status === "failed") return "failed";
    if (["pending", "running", "paused"].includes(run.status)) return "running";
    if (run.status === "succeeded") return "done";
    return "stopped";
  }

  // Every entry of the week, by local day. A run is titled by what it made when it made
  // something, and otherwise by its saved name; below, its one-line result when it has one.
  function entries(view, { timeZone, needsYou = new Set(), names = new Map() }) {
    const runs = view.runs.map((run) => {
      const name = names.get(run.project_workflow_id) || run.workflow_title;
      return {
        kind: "run",
        id: run.id,
        day: localDay(run.at, timeZone),
        at: run.at,
        state: runState(run, needsYou),
        title: run.title || name,
        detail: run.summary || (run.title ? name : run.workflow_title),
      };
    });
    const slots = view.occurrences.map((slot) => ({
      kind: "slot",
      id: slot.project_workflow_id,
      day: localDay(slot.at, timeZone),
      at: slot.at,
      state: slot.state,
      title: slot.name,
      detail: slot.state === "held" ? "Waits for your review first"
        : slot.state === "paused" ? "Paused"
          : slot.state === "skipped" ? "Skipped once"
            : `Lands in ${slot.lands}`,
    }));
    return [...runs, ...slots].sort((a, b) => RANK[a.state] - RANK[b.state] || a.at.localeCompare(b.at));
  }

  function card(entry, timeZone) {
    const target = entry.kind === "run" ? `data-week-run="${escapeHtml(entry.id)}"` : `data-week-slot="${escapeHtml(entry.id)}"`;
    const time = clock(entry.at, timeZone);
    return `<button class="system-week-card is-${entry.state.replace("_", "-")}" type="button" ${target}
      aria-label="${escapeHtml(`${entry.title}, ${LABEL[entry.state]}, ${time}`)}">
      <strong>${escapeHtml(entry.title)}</strong>
      <span>${escapeHtml(entry.detail)}</span>
      <small><span>${escapeHtml(time)}</span><span class="system-week-state">${escapeHtml(LABEL[entry.state])}</span></small>
    </button>`;
  }

  function chip(entry, timeZone) {
    return `<button class="system-week-chip is-${entry.state.replace("_", "-")}" type="button" data-week-run="${escapeHtml(entry.id)}"
      aria-label="${escapeHtml(`${entry.title}, ${LABEL[entry.state]}, ${clock(entry.at, timeZone)}`)}"><span>${escapeHtml(entry.title)}</span></button>`;
  }

  // Finished history is one line; anything that needs a look, and every day from today on,
  // keeps the card.
  const isCard = (entry, past) => !past || ["for_you", "failed", "running"].includes(entry.state);

  function render(view, { today, timeZone, needsYou, names, openDay = null, loading = false }) {
    const all = entries(view, { timeZone, needsYou, names });
    const days = Array.from({ length: 7 }, (_, index) => addDays(view.start, index));
    const columns = days.map((day) => {
      const own = all.filter((entry) => entry.day === day);
      const shown = own.filter((entry) => entry.state !== "stopped").slice(0, VISIBLE);
      const hidden = own.length - shown.length;
      const past = day < today;
      const open = Boolean(hidden) && openDay === day;
      const classes = [
        "system-week-day", day === today ? "is-today" : "", past ? "is-past" : "",
        own.length ? "" : "is-empty", open ? "is-open" : "",
      ];
      return `<div class="${classes.filter(Boolean).join(" ")}" data-week-day="${day}">
        <span class="system-week-date" title="${escapeHtml(dayName(day))}">${escapeHtml(dayLabel(day))}</span>
        ${shown.map((entry) => isCard(entry, past) ? card(entry, timeZone) : chip(entry, timeZone)).join("")}
        ${hidden ? `<button class="system-week-more" type="button" data-week-more="${day}" aria-expanded="${open}">+${hidden} more</button>` : ""}
        ${open ? sheet(own, day, timeZone) : ""}
      </div>`;
    });
    return `<section id="system-week" class="system-week${loading ? " is-loading" : ""}" aria-label="${escapeHtml(`Week of ${dayName(view.start)}`)}" aria-busy="${loading}">
      <div class="system-week-days">${columns.join("")}</div>
    </section>`;
  }

  // A busy day opened: every entry as a card, stopped runs folded into one.
  function sheet(own, day, timeZone) {
    const stopped = own.filter((entry) => entry.state === "stopped");
    const runs = own.filter((entry) => entry.kind === "run").length;
    const slots = own.length - runs;
    const counts = [
      runs ? `${runs} ${runs === 1 ? "run" : "runs"}` : "",
      slots ? `${slots} scheduled` : "",
    ].filter(Boolean).join(" · ");
    const folded = stopped.length
      ? `<div class="system-week-card is-folded"><strong>${stopped.length} stopped</strong>
          <span>${escapeHtml(stopped.map((entry) => entry.title).join(", "))}</span>
          <small><span>${escapeHtml(`${clock(stopped[0].at, timeZone)} – ${clock(stopped[stopped.length - 1].at, timeZone)}`)}</span><span class="system-week-state">stopped</span></small>
        </div>`
      : "";
    return `<div class="system-week-sheet" role="dialog" aria-label="${escapeHtml(dayName(day))}" tabindex="-1">
      <header><span><span class="system-week-sheet-day">${escapeHtml(dayLabel(day))} · </span>${escapeHtml(counts)}</span>
        <button class="system-week-close" type="button" data-week-close aria-label="Close ${escapeHtml(dayName(day))}">
          <svg aria-hidden="true" viewBox="0 0 12 12"><path d="M2.5 7.5 6 4l3.5 3.5" /></svg>
        </button>
      </header>
      <div class="system-week-sheet-cards">
        ${own.filter((entry) => entry.state !== "stopped").map((entry) => card(entry, timeZone)).join("")}${folded}
      </div>
    </div>`;
  }

  window.TinSystemWeek = Object.freeze({ addDays, entries, localDay, render, weekStart });
})();
