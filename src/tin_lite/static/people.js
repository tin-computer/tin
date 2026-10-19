"use strict";

// People (Paper PEOPLE-A1, C1, C1b, E1–E3, N1, N1b and REQ-A): everyone this project keeps a
// record of, as a grid with segments and the newest note, and one person in a side panel. A
// member picks their ICP (which no run replaces), marks a customer, writes notes, forgets them,
// and ticks people to ask to connect from their own LinkedIn. Tin's workflows write the rest,
// and read the notes the next time they score the person.
window.TinPeople = (() => {
  const PAGE = 50;
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const RELATIONSHIP = { connected: "Connected", pending: "Pending", none: "Not connected" };
  // LinkedIn's names for its reactions.
  const REACTIONS = {
    LIKE: "Like", PRAISE: "Celebrate", EMPATHY: "Love", INTEREST: "Insightful",
    APPRECIATION: "Support", ENTERTAINMENT: "Funny",
  };
  const SEGMENTS = [
    { key: "all", label: "All people" },
    { key: "fit", label: "ICP fit" },
    { key: "new", label: "Not connected yet" },
    // Shown once someone has been sent a connection request (#450).
    { key: "invited", label: "Invited" },
    // Shown once a visitor journeys run named someone who used the site or product.
    { key: "site", label: "On the site" },
  ];
  // What a person was on the site when a visitor journeys run last read them.
  const WEB_TIER = { lead: "Lead", new_user: "New user", user: "User" };
  const SORTS = {
    name: (a, b) => a.name.localeCompare(b.name),
    fit: (a, b) => fitOf(a) - fitOf(b),
    touch: (a, b) => touchOf(a) - touchOf(b),
  };
  const NOT_A_FIT = "none";
  const ARROW = '<svg aria-hidden="true" viewBox="0 0 8 10"><path d="M4 1v8M1.2 6.2 4 9l2.8-2.8" /></svg>';
  const OUT = '<svg aria-hidden="true" viewBox="0 0 10 10"><path d="M3 2h5v5M8 2 2.5 7.5" /></svg>';
  const CLOSE = '<svg aria-hidden="true" viewBox="0 0 14 14"><path d="M3 3l8 8M11 3l-8 8" /></svg>';
  const CHEVRON = '<svg class="person-pick-chevron" aria-hidden="true" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" /></svg>';

  let view = null;
  let hover = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const field = (person, key) => person.fields?.[key] || null;
  const fitOf = (person) => {
    const value = field(person, "icp_fit")?.value;
    return typeof value === "number" ? value : -1;
  };
  const touchOf = (person) => (person.last_touch ? Date.parse(person.last_touch.at) : 0);
  const peoplePath = (id = "") => `/api/projects/${encodeURIComponent(view.projectId)}/people${id ? `/${encodeURIComponent(id)}` : ""}`;

  function day(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const label = `${MONTHS[date.getMonth()]} ${date.getDate()}`;
    return date.getFullYear() === new Date().getFullYear() ? label : `${label}, ${date.getFullYear()}`;
  }

  // "ICP 1: founders who need growth done" reads as "ICP 1" beside a score.
  function icpShort(text) {
    if (!text) return "";
    if (text === NOT_A_FIT) return "no ICP";
    const numbered = text.match(/^(ICP\s*\d+)\s*[:–—-]/i);
    if (numbered) return numbered[1];
    return text.length > 22 ? `${text.slice(0, 21)}…` : text;
  }

  function icpText(value) {
    if (!value) return { text: "—", short: "—", muted: true };
    if (value === NOT_A_FIT) return { text: "None", short: "None", muted: true };
    const numbered = value.match(/^(ICP\s*\d+)\s*[:–—-]/i);
    return { text: value, short: numbered ? numbered[1] : value, muted: false };
  }

  // Each member sees their own LinkedIn: a request of theirs waiting to go out reads "Queued",
  // one Tin sent and not yet answered reads with its day, "Invited oct 16".
  function relationshipLabel(person, relationship) {
    if (person.request === "queued") return "Queued";
    if (relationship === "pending" && person.invited_at) return `Invited ${day(person.invited_at)}`;
    return RELATIONSHIP[relationship] || "—";
  }

  // A value a member set, rather than a run: no run replaces it.
  const memberValue = (value) => value.by === "you" || value.by === "a teammate";
  // Who set a value, as the grid marks it beside a score: "you", or a teammate.
  const setMark = (value) => (value.by === "you" ? "you" : "team");
  const firstName = (person) => (person.name || "").split(/\s+/)[0] || "this person";

  function staleNote(value, person) {
    if (value.stale_reason === "note") {
      const newest = person?.notes?.[0] || person?.note;
      const author = newest?.who || newest?.by;
      const whose = newest?.by === "a teammate" ? `${author[0].toUpperCase()}${author.slice(1)}’s note` : "Your note";
      return `${whose} came after this score. The next run reads it and rescores.`;
    }
    return `Scored before ${value.definition || "the ICP file"} or Tin’s scoring changed. The next run rescores it.`;
  }

  // A member's pick, with the score of Tin's it replaced.
  function setNote(value) {
    const who = value.by === "you" ? "Set by you." : "Set by a teammate.";
    const previous = value.previous;
    if (!previous || typeof previous.value !== "number") return who;
    return `${who} Tin scored ${previous.value.toFixed(2)}${previous.reason ? `: ${previous.reason}` : "."}`;
  }

  function mount(main, options) {
    const fresh = !view || view.projectId !== options.projectId;
    if (fresh) {
      close();
      view = {
        ...options, people: null, error: null, fitShown: 0.6, profiles: [], segment: "all", query: "",
        sort: { key: "fit", descending: true }, page: 0, picked: new Set(), queueing: false,
        openId: new URLSearchParams(window.location.search).get("person"),
        person: null, personError: null, confirming: false, forgetting: false, forgetError: null,
        ...personState(),
      };
    } else {
      Object.assign(view, options);
    }
    view.main = main;
    view.active = true;
    renderShell();
    load();
    if (view.openId && !document.getElementById("person-panel")) openPerson(view.openId, { focus: false });
    else renderPanel();
  }

  // What belongs to one open person: an open menu, a save in flight, a note being written.
  function personState() {
    return { menu: null, saving: false, fieldError: null, draft: "", editing: null, editDraft: "", noteError: null };
  }

  async function load() {
    const current = view;
    try {
      const listed = await current.api(peoplePath());
      if (view !== current || !current.active) return;
      current.people = listed.people;
      // Only people the member can still ask stay ticked.
      const open = new Set(listed.people.filter((person) => person.requestable).map((person) => person.id));
      current.picked = new Set([...current.picked].filter((id) => open.has(id)));
      current.fitShown = listed.fit_shown;
      current.profiles = listed.profiles || [];
      current.error = null;
    } catch (error) {
      if (view !== current || !current.active) return;
      current.error = error.message;
    }
    renderShell();
    if (current.openId) renderPanel();
  }

  function segmentPeople(key) {
    const people = view.people || [];
    if (key === "fit") return people.filter((person) => fitOf(person) >= view.fitShown);
    if (key === "new") return people.filter((person) => field(person, "linkedin_relationship")?.value === "none");
    if (key === "invited") return people.filter((person) => field(person, "linkedin_relationship")?.value === "pending");
    if (key === "site") return people.filter((person) => field(person, "web_tier"));
    return people;
  }

  function shown() {
    const query = view.query.trim().toLowerCase();
    const people = segmentPeople(view.segment).filter((person) => !query ||
      [person.name, person.headline, person.email, field(person, "web_summary")?.value, field(person, "icp")?.value, person.notes_text].some((text) => (text || "").toLowerCase().includes(query)));
    const compare = SORTS[view.sort.key];
    return [...people].sort((a, b) => {
      const order = view.sort.descending ? compare(b, a) : compare(a, b);
      return order || a.name.localeCompare(b.name);
    });
  }

  function renderShell() {
    const main = view.main;
    if (!main.querySelector(".people-view")) {
      main.innerHTML = `<section class="product-view workspace-view people-view">
        <header class="workspace-header system-header">
          <h1>People</h1>
          <span class="header-spacer"></span>
          <div class="search-field is-registry">
            <svg aria-hidden="true" viewBox="0 0 13 13"><circle cx="5.5" cy="5.5" r="4" /><path d="M8.5 8.5L12 12" /></svg>
            <input id="people-search" type="search" aria-label="Search people" placeholder="Search people…" />
            <button class="search-clear" type="button" data-clear-people-search aria-label="Clear search" hidden>✕</button>
          </div>
        </header>
        <div class="people-body"></div>
      </section>`;
      const search = main.querySelector("#people-search");
      search.value = view.query;
      search.addEventListener("input", () => {
        view.query = search.value;
        view.page = 0;
        syncSearch();
        renderBody();
      });
      main.querySelector("[data-clear-people-search]").addEventListener("click", () => {
        view.query = "";
        search.value = "";
        view.page = 0;
        syncSearch();
        renderBody();
        search.focus();
      });
    }
    syncSearch();
    renderBody();
  }

  function syncSearch() {
    const box = view.main.querySelector(".people-view .search-field");
    if (!box) return;
    box.hidden = !view.people?.length;
    box.classList.toggle("is-active", Boolean(view.query));
    box.querySelector("[data-clear-people-search]").hidden = !view.query;
  }

  // Ticking people to ask to connect needs the member's own LinkedIn reading on this project.
  function picking() {
    const data = view.requests?.();
    return Boolean(data?.offered && (data.reading || data.browser_only));
  }

  // The bar above the grid while people are ticked (REQ-A, REQ-A3).
  function pickedBar() {
    const count = view.picked.size;
    if (!count || !picking()) return "";
    const { ready, line } = window.TinRequests.picked(view.requests());
    return `<div class="people-picked" role="region" aria-label="Ticked people">
      <strong>${count} selected</strong>
      <button class="people-picked-clear" type="button" data-people-clear-picked>Clear</button>
      <span class="people-picked-space"></span>
      <p>${escape(line)}</p>
      ${ready ? "" : '<button class="requests-settings-link" type="button" data-people-open-linkedin>Open the LinkedIn card</button>'}
      <button class="button people-picked-send" type="button" data-people-send ${ready && !view.queueing ? "" : "disabled"}>${view.queueing ? "Queueing…" : `Send ${count} ${count === 1 ? "request" : "requests"}`}</button>
    </div>`;
  }

  // In the bar's place when nobody on the list has a box, why (REQ-A4).
  function nobodyLine(people) {
    if (!picking() || !people.length || people.some((person) => person.requestable)) return "";
    const why = new Set(people.map((person) => {
      const relationship = field(person, "linkedin_relationship")?.value;
      if (field(person, "customer")?.value === "yes") return "a customer";
      if (relationship === "connected") return "connected to you";
      if (relationship === "pending" || person.request === "queued") return "invited";
      return "";
    }));
    const reasons = ["connected to you", "invited", "a customer"].filter((reason) => why.has(reason));
    const listed = reasons.length > 1 ? `${reasons.slice(0, -1).join(", ")} or ${reasons.at(-1)}` : reasons[0];
    const line = why.has("") ? "No one on this list can be asked right now." : `No one here to ask. Everyone on this list is already ${listed}.`;
    return `<div class="people-picked people-nobody"><p>${escape(line)}</p></div>`;
  }

  function renderBody() {
    const body = view.main.querySelector(".people-body");
    if (!body) return;
    hideHover();
    if (view.error && !view.people) {
      body.innerHTML = `<div class="decisions-empty"><strong>Tin couldn't load people.</strong><span>${escape(view.error)}</span></div>`;
      return;
    }
    if (!view.people) {
      body.innerHTML = '<div class="view-loading">Loading people…</div>';
      return;
    }
    if (!view.people.length) {
      body.innerHTML = `<div class="decisions-empty"><strong>No people yet.</strong><span>When someone reacts to your LinkedIn posts, Tin adds them here with how well they fit your ICP.</span><button class="button-secondary" type="button" data-people-open-system>Open System</button></div>`;
      body.querySelector("[data-people-open-system]").addEventListener("click", () => view.openSystem?.());
      return;
    }
    const people = shown();
    const pages = Math.max(1, Math.ceil(people.length / PAGE));
    view.page = Math.min(view.page, pages - 1);
    const start = view.page * PAGE;
    const visible = people.slice(start, start + PAGE);
    const pick = picking();
    const pickable = visible.filter((person) => person.requestable);
    const ticked = pickable.filter((person) => view.picked.has(person.id)).length;
    body.innerHTML = `
      <div class="workflow-sections system-sections people-sections" role="tablist" aria-label="People segments">
        ${SEGMENTS.filter((segment) => !["invited", "site"].includes(segment.key) || view.segment === segment.key || segmentPeople(segment.key).length).map((segment) => `<button class="workflow-section ${view.segment === segment.key ? "is-active" : ""}" type="button" role="tab" aria-selected="${view.segment === segment.key}" data-people-segment="${segment.key}">${segment.label} <span>${segmentPeople(segment.key).length}</span></button>`).join("")}
      </div>
      ${pickedBar() || nobodyLine(people)}
      <div class="people-grid">
        <table class="tin-table people-table ${pick ? "has-pick" : ""}">
          <thead><tr>
            ${pick ? `<th class="is-pick" scope="col"><input type="checkbox" data-people-pick-all aria-label="Tick everyone on this page you can ask to connect" ${pickable.length ? "" : "disabled"} ${pickable.length && ticked === pickable.length ? "checked" : ""} /></th>` : ""}
            ${header("name", "Name")}
            <th class="is-headline" scope="col">Headline</th>
            <th class="is-icp" scope="col">ICP</th>
            ${header("fit", "ICP fit")}
            <th class="is-notes" scope="col">Notes</th>
            <th class="is-linkedin" scope="col">LinkedIn</th>
            ${header("touch", "Last touch")}
          </tr></thead>
          <tbody>${visible.length ? visible.map((person) => row(person, pick)).join("") : `<tr class="is-none"><td colspan="${pick ? 8 : 7}">${view.query.trim() ? `No one matches “${escape(view.query.trim())}”.` : "No one here yet."}</td></tr>`}</tbody>
        </table>
      </div>
      <footer class="people-pager">
        <code>${people.length ? `${start + 1}–${start + visible.length} of ${people.length}` : "0 of 0"}</code>
        <span></span>
        ${pages > 1 ? `<div>
          <button type="button" data-people-page="-1" aria-label="Previous page" ${view.page === 0 ? "disabled" : ""}><svg aria-hidden="true" viewBox="0 0 12 12"><path d="M7.5 2.5 4 6l3.5 3.5" /></svg></button>
          <button type="button" data-people-page="1" aria-label="Next page" ${view.page >= pages - 1 ? "disabled" : ""}><svg aria-hidden="true" viewBox="0 0 12 12"><path d="M4.5 2.5 8 6l-3.5 3.5" /></svg></button>
        </div>` : ""}
      </footer>`;
    const all = body.querySelector("[data-people-pick-all]");
    if (all) all.indeterminate = ticked > 0 && ticked < pickable.length;
    bindBody(body, pickable);
  }

  function header(key, label) {
    const active = view.sort.key === key;
    const sort = active ? (view.sort.descending ? "descending" : "ascending") : "none";
    return `<th class="is-${key} is-sortable" scope="col" aria-sort="${sort}">
      <button type="button" data-people-sort="${key}" class="${active ? "is-active" : ""} ${active && !view.sort.descending ? "is-ascending" : ""}">${label}${active ? ARROW : ""}</button>
    </th>`;
  }

  function row(person, pick = false) {
    const icp = field(person, "icp");
    const fit = field(person, "icp_fit");
    const relationship = field(person, "linkedin_relationship")?.value;
    const customer = field(person, "customer")?.value === "yes";
    const icpLabel = icpText(icp?.value);
    const fitValue = typeof fit?.value === "number" ? fit.value : null;
    const memberSet = fit && memberValue(fit);
    const fitClass = fitValue === null || fit.stale ? "is-muted" : fitValue >= view.fitShown ? "" : "is-secondary";
    const mark = fit?.stale && fitValue !== null ? "old" : memberSet ? setMark(fit) : "";
    const touch = person.last_touch;
    const note = person.note;
    const picked = view.picked.has(person.id);
    const box = pick
      ? `<td class="is-pick">${person.requestable ? `<input type="checkbox" data-people-pick="${escape(person.id)}" aria-label="Ask ${escape(person.name || "this person")} to connect" ${picked ? "checked" : ""} />` : ""}</td>`
      : "";
    return `<tr tabindex="0" data-person="${escape(person.id)}" class="${person.id === view.openId ? "is-selected" : ""} ${picked ? "is-picked" : ""}" aria-label="${escape(person.name || "Unnamed")}">
      ${box}<td class="is-name"><span class="people-name"><span>${escape(person.name || "Unnamed")}</span>${customer ? "<small>customer</small>" : ""}</span></td>
      <td class="is-headline"><span>${escape(person.headline || field(person, "web_summary")?.value || "")}</span></td>
      <td class="is-icp ${icpLabel.muted ? "is-muted" : ""}" title="${escape(icpLabel.muted ? "" : icpLabel.text)}"><span class="is-short">${escape(icpLabel.short)}</span><span class="is-full">${escape(icpLabel.text)}</span></td>
      <td class="is-fit ${fitClass}" ${fit ? `data-fit="${escape(person.id)}"` : ""}>${mark ? `<small>${mark}</small>` : ""}<span>${fitValue === null ? "—" : fitValue.toFixed(2)}</span></td>
      <td class="is-notes" ${note ? `data-note="${escape(person.id)}"` : ""}><span>${note ? escape(note.body.split("\n")[0]) : ""}</span></td>
      <td class="is-linkedin"><span>${escape(relationshipLabel(person, relationship))}</span></td>
      <td class="is-touch"><span>${touch ? `${escape(day(touch.at))} · ${escape(touchText(touch))}` : "—"}</span></td>
    </tr>`;
  }

  function bindBody(body, pickable = []) {
    body.querySelectorAll("[data-people-segment]").forEach((button) => button.addEventListener("click", () => {
      view.segment = button.dataset.peopleSegment;
      view.page = 0;
      renderBody();
    }));
    body.querySelectorAll("[data-people-sort]").forEach((button) => button.addEventListener("click", () => {
      const key = button.dataset.peopleSort;
      view.sort = view.sort.key === key ? { key, descending: !view.sort.descending } : { key, descending: key !== "name" };
      view.page = 0;
      renderBody();
    }));
    body.querySelectorAll("[data-people-page]").forEach((button) => button.addEventListener("click", () => {
      view.page += Number(button.dataset.peoplePage);
      renderBody();
      body.querySelector(".people-grid")?.scrollIntoView({ block: "nearest" });
    }));
    body.querySelectorAll("[data-people-pick]").forEach((box) => {
      // Ticking someone doesn't open them.
      box.closest("td").addEventListener("click", (event) => event.stopPropagation());
      box.addEventListener("keydown", (event) => event.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) view.picked.add(box.dataset.peoplePick);
        else view.picked.delete(box.dataset.peoplePick);
        renderBody();
      });
    });
    body.querySelector("[data-people-pick-all]")?.addEventListener("change", (event) => {
      const every = pickable.every((person) => view.picked.has(person.id));
      pickable.forEach((person) => (every ? view.picked.delete(person.id) : view.picked.add(person.id)));
      event.target.checked = !every;
      renderBody();
    });
    body.querySelector("[data-people-clear-picked]")?.addEventListener("click", () => {
      view.picked.clear();
      renderBody();
    });
    body.querySelector("[data-people-open-linkedin]")?.addEventListener("click", () => view.openLinkedInCard?.());
    body.querySelector("[data-people-send]")?.addEventListener("click", () => sendRequests());
    body.querySelectorAll("tbody tr[data-person]").forEach((tr) => {
      tr.addEventListener("click", () => openPerson(tr.dataset.person));
      tr.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        openPerson(tr.dataset.person);
      });
    });
    body.querySelectorAll("td[data-fit]").forEach((cell) => {
      cell.addEventListener("pointerenter", () => showHover(cell, fitHover(cell.dataset.fit)));
      cell.addEventListener("pointerleave", hideHover);
    });
    body.querySelectorAll("td[data-note]").forEach((cell) => {
      cell.addEventListener("pointerenter", () => showHover(cell, noteHover(cell.dataset.note)));
      cell.addEventListener("pointerleave", hideHover);
    });
  }

  // The reason behind a score, its lineage, and whether it's out of date (PEOPLE-A1 hover).
  function fitHover(id) {
    const person = view.people?.find((item) => item.id === id);
    const fit = person && field(person, "icp_fit");
    if (!fit) return "";
    const icp = field(person, "icp");
    const line = [typeof fit.value === "number" ? fit.value.toFixed(2) : "", icpShort(icp?.value), day(fit.at)].filter(Boolean).join(" · ");
    const reason = memberValue(fit) ? setNote(fit) : fit.reason;
    return `${reason ? `<p>${escape(reason)}</p>` : ""}<code>${escape(line)}</code>${fit.stale ? `<small>${escape(staleNote(fit, person))}</small>` : ""}`;
  }

  // The newest note in full (PEOPLE-N1b hover).
  function noteHover(id) {
    const note = view.people?.find((item) => item.id === id)?.note;
    if (!note) return "";
    const line = [note.who || note.by, day(note.at), note.count > 1 ? `latest of ${note.count} notes` : ""].filter(Boolean).join(" · ");
    return `<p class="is-note">${escape(note.body)}</p><code>${escape(line)}</code>`;
  }

  function showHover(cell, html) {
    if (!html) return;
    hover ||= Object.assign(document.createElement("div"), { className: "people-hover", role: "tooltip" });
    if (!hover.isConnected) document.body.append(hover);
    hover.innerHTML = html;
    // Under the cell, its right edge on the cell's.
    const rect = cell.getBoundingClientRect();
    hover.style.left = `${Math.max(16, rect.right - 300)}px`;
    hover.style.top = `${rect.bottom - 4}px`;
    hover.classList.add("is-visible");
  }

  function hideHover() {
    hover?.classList.remove("is-visible");
  }

  function setPersonParam(id) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("person", id);
    else url.searchParams.delete("person");
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  }

  async function openPerson(id, { focus = true } = {}) {
    const current = view;
    const switching = current.openId !== id;
    current.openId = id;
    current.confirming = false;
    current.forgetError = null;
    if (switching || !current.person || current.person.id !== id) {
      current.person = current.people?.find((item) => item.id === id) || null;
      current.personError = null;
      Object.assign(current, personState());
    }
    setPersonParam(id);
    renderPanel();
    syncSelection();
    if (focus) document.getElementById("person-panel")?.focus({ preventScroll: true });
    try {
      const person = await current.api(peoplePath(id));
      if (view !== current || !current.active || current.openId !== id) return;
      current.person = person;
    } catch (error) {
      if (view !== current || !current.active || current.openId !== id) return;
      current.personError = error.message;
    }
    renderPanel();
  }

  function closePerson({ restoreFocus = true } = {}) {
    if (!view?.openId) return;
    const id = view.openId;
    view.openId = null;
    view.person = null;
    view.confirming = false;
    Object.assign(view, personState());
    setPersonParam(null);
    renderPanel();
    syncSelection();
    if (restoreFocus) view.main.querySelector(`tr[data-person="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
  }

  function syncSelection() {
    view.main.querySelectorAll("tbody tr[data-person]").forEach((tr) => tr.classList.toggle("is-selected", tr.dataset.person === view.openId));
  }

  function fieldRow(label, value, lineage, extra = "", key = "") {
    return `<div class="person-field ${view.menu === key && key ? "is-open" : ""}" ${key ? `data-field="${key}"` : ""}>
      <div class="person-field-line"><span>${label}</span>${value}${lineage && view.menu !== key ? `<code>${escape(lineage)}</code>` : ""}</div>
      ${extra}
    </div>`;
  }

  function lineage(value) {
    return value ? [value.by, day(value.at)].filter(Boolean).join(" · ") : "";
  }

  // A value a member can change: it reads as text and opens a menu (PEOPLE-E1).
  function pick(key, label, muted) {
    const open = view.menu === key;
    return `<button class="person-pick ${muted ? "is-muted" : ""} ${open ? "is-open" : ""}" type="button" data-pick="${key}" aria-haspopup="listbox" aria-expanded="${open}" ${view.saving ? "disabled" : ""}><span>${escape(label)}</span>${CHEVRON}</button>`;
  }

  function menu(key, options, note = "") {
    if (view.menu !== key) return "";
    return `<div class="tin-select-menu person-menu" role="listbox" aria-label="${key === "icp" ? "ICP" : "Customer"}">
      ${options.map((option) => `<button class="tin-select-option ${option.selected ? "is-selected" : ""}" type="button" role="option" aria-selected="${Boolean(option.selected)}" data-choose="${escape(option.value)}"><span>${escape(option.label)}</span>${option.tag ? `<code>${escape(option.tag)}</code>` : '<span class="tin-select-marker" aria-hidden="true"></span>'}</button>`).join("")}
      ${note ? `<p>${escape(note)}</p>` : ""}
    </div>`;
  }

  function fieldsHtml(person) {
    const rows = [];
    const icp = field(person, "icp");
    const fit = field(person, "icp_fit");
    const relationship = field(person, "linkedin_relationship");
    const customer = field(person, "customer");
    const memberFit = fit && memberValue(fit);
    const profiles = view.profiles || [];
    if (icp || profiles.length) {
      const label = icpText(icp?.value);
      // Tin's own pick is tagged with its score; a member's pick is the selected one.
      const tinScore = !memberFit && fit && typeof fit.value === "number" ? `Tin ${fit.value.toFixed(2)}` : "";
      const options = [...profiles, NOT_A_FIT].map((value) => ({
        value,
        label: value === NOT_A_FIT ? "Not a fit" : value,
        tag: !memberFit && icp?.value === value ? tinScore : "",
        selected: memberFit && icp?.value === value,
      }));
      const value = profiles.length ? pick("icp", label.text, label.muted) : `<strong class="${label.muted ? "is-muted" : ""}">${escape(label.text)}</strong>`;
      rows.push(fieldRow("ICP", value, lineage(icp), menu("icp", options, "Your pick replaces Tin’s score, and later runs keep it."), "icp"));
    }
    if (fit) {
      const number = typeof fit.value === "number" ? fit.value.toFixed(2) : "—";
      const value = `<strong class="is-mono ${fit.stale || fit.value === null ? "is-muted" : ""}">${fit.stale && fit.value !== null ? "<small>old</small> " : ""}${number}</strong>`;
      const lines = [];
      if (memberFit) {
        lines.push(`<p>${escape(setNote(fit))}</p>`);
        lines.push(`<button class="person-field-action" type="button" data-use-tin-score ${view.saving ? "disabled" : ""}>Use Tin’s score</button>`);
      } else {
        if (fit.reason) lines.push(`<p>${escape(fit.reason)}</p>`);
        if (fit.stale) lines.push(`<p class="is-quiet">${escape(staleNote(fit, person))}</p>`);
      }
      rows.push(fieldRow("ICP fit", value, lineage(fit), lines.length ? `<div class="person-field-notes">${lines.join("")}</div>` : ""));
    }
    // A request of the viewer's waiting to go out says when, and can be cancelled (REQ-C2).
    const queued = person.request === "queued";
    if (relationship || queued) {
      const notes = queued
        ? `<div class="person-field-notes"><p>${escape(window.TinRequests.waitingText(person.queued, view.requests?.()))}</p>${person.queued?.sending ? "" : `<button class="person-field-action is-quiet" type="button" data-cancel-request ${view.saving ? "disabled" : ""}>Cancel request</button>`}</div>`
        : "";
      const since = queued && person.queued ? `you · ${day(person.queued.queued_at)}` : lineage(relationship);
      rows.push(fieldRow("LinkedIn", `<strong>${escape(relationshipLabel(person, relationship?.value))}</strong>`, since, notes));
    }
    const web = field(person, "web_tier");
    if (web) {
      const summary = field(person, "web_summary");
      const lines = [summary?.value, web.reason ? `Stopped at ${web.reason}` : ""].filter(Boolean);
      rows.push(fieldRow(
        "On the site",
        `<strong>${escape(WEB_TIER[web.value] || web.value)}</strong>`,
        lineage(web),
        lines.length ? `<div class="person-field-notes">${lines.map((line) => `<p>${escape(line)}</p>`).join("")}</div>` : "",
      ));
    }
    const isCustomer = customer?.value === "yes";
    rows.push(fieldRow(
      "Customer",
      pick("customer", isCustomer ? "Yes" : "No", !isCustomer),
      customer ? lineage(customer) : "",
      [
        menu("customer", [
          { value: "yes", label: "Yes", selected: isCustomer },
          { value: "no", label: "No", selected: Boolean(customer) && !isCustomer },
        ]),
        isCustomer ? '<div class="person-field-notes"><p>Tin leaves customers out of invitations and reach lists.</p></div>' : "",
      ].join(""),
      "customer",
    ));
    const error = view.fieldError ? `<p class="person-error" role="alert">${escape(view.fieldError)}</p>` : "";
    return `<section class="person-section"><h3>Fields <span>${rows.length}</span></h3>${rows.join("")}${error}</section>`;
  }

  // Notes the team wrote about the person, newest first (PEOPLE-N1).
  function notesHtml(person) {
    if (!person.notes) return "";
    const writing = view.draft.trim().length > 0;
    const composer = `<div class="person-note-composer ${writing ? "is-writing" : ""}">
      <textarea data-note-draft rows="${writing ? 3 : 1}" aria-label="Add a note" placeholder="Add a note about ${escape(firstName(person))}…" maxlength="2000">${escape(view.draft)}</textarea>
      ${writing ? `<div class="person-note-actions">
        <button class="person-note-cancel" type="button" data-note-cancel>Cancel</button>
        <button class="button person-note-save" type="button" data-note-save ${view.saving ? "disabled" : ""}>${view.saving ? "Saving…" : "Save note"}</button>
      </div>` : ""}
    </div>`;
    const notes = person.notes.map((note) => {
      if (view.editing === note.id) {
        return `<div class="person-note is-editing" data-note-id="${escape(note.id)}">
          <textarea data-note-edit rows="3" aria-label="Edit note" maxlength="2000">${escape(view.editDraft)}</textarea>
          <div class="person-note-actions">
            <button class="person-note-cancel" type="button" data-note-edit-cancel>Cancel</button>
            <button class="button person-note-save" type="button" data-note-edit-save ${view.saving ? "disabled" : ""}>${view.saving ? "Saving…" : "Save"}</button>
          </div>
        </div>`;
      }
      const meta = [note.who || note.by, day(note.at), note.edited ? "edited" : ""].filter(Boolean).join(" · ");
      return `<div class="person-note" data-note-id="${escape(note.id)}">
        <p>${escape(note.body)}</p>
        <div class="person-note-meta"><code>${escape(meta)}</code>${note.mine ? `<span>
          <button type="button" data-note-edit-start ${view.saving ? "disabled" : ""}>Edit</button>
          <button type="button" data-note-delete ${view.saving ? "disabled" : ""}>Delete</button>
        </span>` : ""}</div>
      </div>`;
    }).join("");
    const error = view.noteError ? `<p class="person-error" role="alert">${escape(view.noteError)}</p>` : "";
    return `<section class="person-section person-notes"><h3>Notes ${person.notes.length ? `<span>${person.notes.length}</span>` : ""}</h3>${composer}${error}${notes}</section>`;
  }

  // Who was behind an event, for the viewer: "you", a teammate's first name, or "a teammate"
  // (TEAM-A, TEAM-C). Older answers only say whether it was yours.
  const whoOf = (item) => item.who || (item.yours === true ? "you" : item.yours === false ? "a teammate" : null);
  const possessive = (who) => (who === "you" ? "your" : `${who}’s`);

  // The grid's Last touch: "reacted to you", "invited by Mira".
  function touchText(touch) {
    const who = whoOf(touch);
    if (!who) return touch.kind;
    if (touch.kind === "reacted") return `reacted to ${who}`;
    if (touch.kind === "commented") return `replied to ${who}`;
    if (touch.kind === "invited") return `invited by ${who}`;
    if (touch.kind === "accepted") return `connected with ${who}`;
    return touch.kind;
  }

  function eventText(event) {
    const who = whoOf(event);
    const whose = who ? `${possessive(who)} post` : "a post";
    if (event.kind === "reacted") {
      const reaction = REACTIONS[event.reaction];
      return `Reacted to ${whose}${reaction ? ` with ${reaction}` : ""}`;
    }
    if (event.kind === "commented") return `Commented on ${whose}`;
    // A request is the member's whose LinkedIn sent it.
    if (event.kind === "invited") return who && who !== "you" ? `${who[0].toUpperCase()}${who.slice(1)} sent a connection request` : "Connection request sent";
    if (event.kind === "accepted") return who && who !== "you" ? `Accepted ${possessive(who)} connection request` : "Accepted your connection request";
    if (event.kind === "withdrawn") return "Connection request withdrawn";
    if (event.kind === "visited") return event.summary ? `Visited: ${event.summary}` : "Visited the site";
    return event.kind;
  }

  function timelineHtml(person) {
    if (!person.events) return view.personError ? "" : '<section class="person-section"><h3>Timeline</h3><p class="person-quiet">Loading…</p></section>';
    if (!person.events.length) return "";
    const rows = person.events.map((event) => {
      const content = `<code>${escape(day(event.at))}</code><span>${escape(eventText(event))}</span>`;
      // A visit opens its PostHog replay; the suggested next step reads under it.
      const link = event.post_url || event.replay_url;
      const shown = link
        ? `<a class="person-event" href="${escape(link)}" target="_blank" rel="noopener noreferrer">${content}${OUT}</a>`
        : `<div class="person-event">${content}</div>`;
      return event.action ? `${shown}<p class="person-event-note">Suggested: ${escape(event.action)}</p>` : shown;
    }).join("");
    return `<section class="person-section"><h3>Timeline <span>${person.events.length}</span></h3>${rows}</section>`;
  }

  function footerHtml(person) {
    if (view.confirming) {
      const name = person.name || "this person";
      const files = person.added?.by === "engagers" ? " The list in Files drops them at the next run." : "";
      return `<footer class="person-forget">
        <div>
          <strong>Forget ${escape(name)}?</strong>
          <p>Tin deletes this person’s fields, notes and timeline in ${escape(view.projectName || "this project")} and won’t add them again, even when they ${person.added?.by === "visitors" ? "come back to the site" : "react to a later post"}.${files}</p>
          ${view.forgetError ? `<p class="person-forget-error" role="alert">${escape(view.forgetError)}</p>` : ""}
        </div>
        <div class="person-forget-actions">
          <button type="button" data-person-cancel ${view.forgetting ? "disabled" : ""}>Cancel</button>
          <button class="person-forget-confirm" type="button" data-person-confirm ${view.forgetting ? "disabled" : ""}>${view.forgetting ? "Forgetting…" : "Forget"}</button>
        </div>
      </footer>`;
    }
    const added = person.added ? `added ${day(person.added.at)} by ${person.added.by}` : "";
    return `<footer><code>${escape(added)}</code><button class="person-forget-start" type="button" data-person-forget>Forget this person</button></footer>`;
  }

  function renderPanel() {
    let panel = document.getElementById("person-panel");
    if (!view?.openId) {
      panel?.remove();
      document.body.classList.remove("has-person-panel");
      return;
    }
    const person = view.person;
    if (!panel) {
      panel = document.createElement("aside");
      panel.id = "person-panel";
      panel.className = "person-panel";
      panel.tabIndex = -1;
      panel.setAttribute("aria-labelledby", "person-panel-name");
      panel.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        // Escape closes what is open inside the panel first: a menu, then a note being edited.
        if (view.menu) {
          const key = view.menu;
          view.menu = null;
          renderPanel();
          document.querySelector(`#person-panel [data-pick="${key}"]`)?.focus();
        } else if (view.editing) {
          view.editing = null;
          renderPanel();
        } else if (event.target.matches("[data-note-draft]") && view.draft.trim()) {
          // A note being written stays; Escape only leaves the box.
          event.target.blur();
        } else {
          closePerson();
        }
      });
      panel.addEventListener("pointerdown", (event) => {
        if (view.menu && !event.target.closest(".person-field.is-open, [data-pick]")) {
          view.menu = null;
          renderPanel();
        }
      });
      document.body.append(panel);
    }
    document.body.classList.add("has-person-panel");
    const scroll = panel.querySelector(".person-panel-body")?.scrollTop || 0;
    if (!person) {
      panel.innerHTML = `<header><div class="person-panel-title"><h2 id="person-panel-name">${view.personError ? "Not found" : "Loading…"}</h2><button class="diagram-panel-close" type="button" data-close-person aria-label="Close this person">${CLOSE}</button></div>
        ${view.personError ? `<p>${escape(view.personError)}</p>` : ""}</header>`;
    } else {
      const body = [fieldsHtml(person), notesHtml(person), timelineHtml(person)].join("");
      panel.innerHTML = `<header>
          <div class="person-panel-title"><h2 id="person-panel-name">${escape(person.name || "Unnamed")}</h2><button class="diagram-panel-close" type="button" data-close-person aria-label="Close this person">${CLOSE}</button></div>
          ${person.headline ? `<p>${escape(person.headline)}</p>` : ""}
          ${person.email ? `<p class="person-panel-email">${escape(person.email)}</p>` : ""}
          ${person.profile_url ? `<a class="person-panel-linkedin" href="${escape(person.profile_url)}" target="_blank" rel="noopener noreferrer">Open on LinkedIn${OUT}</a>` : ""}
        </header>
        <div class="person-panel-body">${body}${view.personError ? `<p class="person-quiet">${escape(view.personError)}</p>` : ""}</div>
        ${footerHtml(person)}`;
      const scroller = panel.querySelector(".person-panel-body");
      if (scroller) scroller.scrollTop = scroll;
    }
    bindPanel(panel, person);
  }

  function bindPanel(panel, person) {
    panel.querySelector("[data-close-person]").addEventListener("click", () => closePerson());
    panel.querySelector("[data-person-forget]")?.addEventListener("click", () => {
      view.confirming = true;
      view.forgetError = null;
      renderPanel();
      panel.querySelector("[data-person-cancel]")?.focus();
    });
    panel.querySelector("[data-person-cancel]")?.addEventListener("click", () => {
      view.confirming = false;
      view.forgetError = null;
      renderPanel();
      panel.querySelector("[data-person-forget]")?.focus();
    });
    panel.querySelector("[data-person-confirm]")?.addEventListener("click", () => forget(person));
    if (!person) return;

    panel.querySelectorAll("[data-pick]").forEach((button) => button.addEventListener("click", () => {
      const key = button.dataset.pick;
      view.menu = view.menu === key ? null : key;
      view.fieldError = null;
      renderPanel();
      document.querySelector(`#person-panel .person-field.is-open .tin-select-option`)?.focus();
    }));
    panel.querySelectorAll("[data-choose]").forEach((option) => option.addEventListener("click", () => {
      const key = view.menu;
      const value = option.dataset.choose;
      view.menu = null;
      if (key === "icp") change({ fit: { icp: value === NOT_A_FIT ? null : value } });
      else if (key === "customer") change({ customer: value === "yes" });
    }));
    panel.querySelectorAll(".person-menu").forEach((list) => list.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      const options = [...list.querySelectorAll(".tin-select-option")];
      const index = options.indexOf(document.activeElement);
      const next = (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options[next]?.focus();
    }));
    panel.querySelector("[data-use-tin-score]")?.addEventListener("click", () => change({ fit: null }));
    panel.querySelector("[data-cancel-request]")?.addEventListener("click", cancelRequest);

    const draft = panel.querySelector("[data-note-draft]");
    draft?.addEventListener("input", () => {
      const was = view.draft.trim().length > 0;
      view.draft = draft.value;
      // The buttons appear with the first words, and go with the last.
      if (was !== view.draft.trim().length > 0) {
        renderPanel();
        const again = document.querySelector("#person-panel [data-note-draft]");
        again?.focus();
        again?.setSelectionRange(again.value.length, again.value.length);
      }
    });
    draft?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        saveNote();
      }
    });
    panel.querySelector("[data-note-save]")?.addEventListener("click", saveNote);
    panel.querySelector("[data-note-cancel]")?.addEventListener("click", () => {
      view.draft = "";
      view.noteError = null;
      renderPanel();
    });
    panel.querySelectorAll("[data-note-edit-start]").forEach((button) => button.addEventListener("click", () => {
      const id = button.closest("[data-note-id]").dataset.noteId;
      view.editing = id;
      view.editDraft = person.notes.find((note) => note.id === id)?.body || "";
      view.noteError = null;
      renderPanel();
      const box = document.querySelector("#person-panel [data-note-edit]");
      box?.focus();
      box?.setSelectionRange(box.value.length, box.value.length);
    }));
    const edit = panel.querySelector("[data-note-edit]");
    edit?.addEventListener("input", () => { view.editDraft = edit.value; });
    edit?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        saveEdit();
      }
    });
    panel.querySelector("[data-note-edit-save]")?.addEventListener("click", saveEdit);
    panel.querySelector("[data-note-edit-cancel]")?.addEventListener("click", () => {
      view.editing = null;
      renderPanel();
    });
    panel.querySelectorAll("[data-note-delete]").forEach((button) => button.addEventListener("click", () => {
      const id = button.closest("[data-note-id]").dataset.noteId;
      write(`${peoplePath(person.id)}/notes/${encodeURIComponent(id)}`, { method: "DELETE" }, "noteError", "Note deleted.");
    }));
  }

  function change(body) {
    write(peoplePath(view.person.id), { method: "PATCH", body: JSON.stringify(body) }, "fieldError");
  }

  function saveNote() {
    if (!view.draft.trim() || view.saving) return;
    write(`${peoplePath(view.person.id)}/notes`, { method: "POST", body: JSON.stringify({ body: view.draft }) }, "noteError")
      .then((saved) => { if (saved) view.draft = ""; renderPanel(); });
  }

  function saveEdit() {
    if (!view.editing || view.saving) return;
    write(`${peoplePath(view.person.id)}/notes/${encodeURIComponent(view.editing)}`, { method: "PATCH", body: JSON.stringify({ body: view.editDraft }) }, "noteError")
      .then((saved) => { if (saved) view.editing = null; renderPanel(); });
  }

  // One change to the open person. The answer is the person as they now are, which also
  // updates their row in the grid.
  async function write(path, options, errorKey, done = "") {
    const current = view;
    const id = current.openId;
    current.saving = true;
    current[errorKey] = null;
    renderPanel();
    let person;
    try {
      person = await current.api(path, options);
    } catch (error) {
      if (view !== current) return false;
      current.saving = false;
      current[errorKey] = error.message;
      if (current.active && current.openId === id) renderPanel();
      return false;
    }
    if (view !== current) return false;
    current.saving = false;
    if (current.openId === id) current.person = person;
    const notes = person.notes || [];
    current.people = (current.people || []).map((item) => (item.id !== person.id ? item : {
      ...item,
      fields: person.fields,
      requestable: person.requestable,
      request: person.request,
      note: notes[0] ? { body: notes[0].body, by: notes[0].by, at: notes[0].at, count: notes.length } : null,
      notes_text: notes.map((note) => note.body).join(" \n"),
    }));
    if (done) current.toast?.(done);
    if (!person.requestable) current.picked.delete(person.id);
    if (!current.active) return true;
    renderBody();
    syncSelection();
    renderPanel();
    return true;
  }

  // Before it goes, the viewer can take their request back; the person can be ticked again.
  async function cancelRequest() {
    const current = view;
    const id = current.openId;
    if (current.saving || !id) return;
    current.saving = true;
    current.fieldError = null;
    renderPanel();
    try {
      await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/linkedin-requests/people/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    } catch (error) {
      if (view !== current) return;
      current.saving = false;
      current.fieldError = error.message;
      if (current.active && current.openId === id) renderPanel();
      return;
    }
    if (view !== current) return;
    current.saving = false;
    await write(peoplePath(id), undefined, "fieldError", "Request cancelled. Tin won’t send it.");
    current.refreshRequests?.();
  }

  // Ticking is the approval: the people ticked are queued for Tin to send one at a time.
  async function sendRequests() {
    const current = view;
    if (current.queueing || !current.picked.size) return;
    // In the order they show, best fit first.
    const order = new Map(shown().map((person, index) => [person.id, index]));
    const people = [...current.picked].sort((a, b) => (order.get(a) ?? 1e9) - (order.get(b) ?? 1e9));
    current.queueing = true;
    renderBody();
    let answer;
    try {
      answer = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/linkedin-requests/send`, {
        method: "POST",
        body: JSON.stringify({ request_id: window.crypto.randomUUID(), people }),
      });
    } catch (error) {
      if (view !== current) return;
      current.queueing = false;
      current.toast?.(error.message);
      if (current.active) renderBody();
      return;
    }
    if (view !== current) return;
    current.queueing = false;
    current.picked.clear();
    const queued = new Set(people);
    current.people = (current.people || []).map((item) => (queued.has(item.id) ? { ...item, requestable: false, request: "queued" } : item));
    current.toast?.(window.TinRequests.queuedText(answer));
    if (current.active) renderBody();
    current.refreshRequests?.();
  }

  async function forget(person) {
    const current = view;
    current.forgetting = true;
    current.forgetError = null;
    renderPanel();
    try {
      await current.api(peoplePath(person.id), { method: "DELETE" });
    } catch (error) {
      if (view !== current) return;
      current.forgetting = false;
      current.forgetError = error.message;
      if (current.active) renderPanel();
      return;
    }
    if (view !== current) return;
    current.forgetting = false;
    current.people = (current.people || []).filter((item) => item.id !== person.id);
    current.picked.delete(person.id);
    current.toast?.(`Forgot ${person.name || "this person"}.`);
    if (!current.active) return;
    closePerson({ restoreFocus: false });
    renderBody();
  }

  // Leaving the view: the open person, the panel and the hover card go with it.
  function close() {
    hideHover();
    if (view) Object.assign(view, { active: false, openId: null, person: null, personError: null, confirming: false, ...personState() });
    document.getElementById("person-panel")?.remove();
    document.body.classList.remove("has-person-panel");
  }

  window.addEventListener("scroll", hideHover, { passive: true });

  return { mount, close };
})();
