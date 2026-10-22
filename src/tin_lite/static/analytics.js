"use strict";

// Analytics (Paper REP-V2, V2b, V2c, V2d and V2e): who visits the project's site, from Tin's
// stored copy of the report Agent Traffic builds. Today, Yesterday and the last 7 days, each
// number beside its change from the period before; while no visit has arrived, the waiting page
// with the script line. Every number comes from the report and is exact; a null is a dash.
window.TinAnalytics = (() => {
  const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  const TABS = [
    { key: "today", label: "Today" },
    { key: "yesterday", label: "Yesterday" },
    { key: "week", label: "Last 7 days" },
  ];
  const ROLES = { user_retrieval: "fetching for someone", indexing: "search index", training: "training", other: "other" };
  // The report names a company by one of Agent Traffic's operator IDs. An ID added after these
  // stays as it is.
  const COMPANIES = {
    openai: "OpenAI", anthropic: "Anthropic", google: "Google", microsoft: "Microsoft", apple: "Apple",
    perplexity: "Perplexity", mistral: "Mistral", commoncrawl: "Common Crawl", duckduckgo: "DuckDuckGo",
    amazon: "Amazon", keenable: "Keenable", "you.com": "You.com", exa: "Exa", meta: "Meta", ahrefs: "Ahrefs",
  };
  // Every count is exact; a dash stands in if one ever comes back null.
  const DASH = "–";

  let view = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const count = (value) => (typeof value === "number" ? value.toLocaleString("en-US") : DASH);
  const company = (id) => COMPANIES[id] || id;
  const host = (address) => {
    try {
      return new URL(address).hostname;
    } catch (_error) {
      return "";
    }
  };

  // Dates in the report are the founder's days; times are shown in their time zone.
  function day(value) {
    const [year, month, date] = String(value || "").split("-").map(Number);
    return year ? new Date(Date.UTC(year, month - 1, date)) : null;
  }
  const dayLabel = (value) => {
    const date = day(value);
    return date ? `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}` : "";
  };
  const weekday = (value) => {
    const date = day(value);
    return date ? WEEKDAYS[date.getUTCDay()] : "";
  };
  function clock(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return "";
    try {
      return new Intl.DateTimeFormat("en-GB", { timeZone: view.data.tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(date);
    } catch (_error) {
      return "";
    }
  }
  function zoneLabel(tz) {
    if (!tz || tz === "UTC") return "utc";
    return `${tz.split("/").pop().replace(/_/g, " ").toLowerCase()} time`;
  }

  function mount(main, options) {
    const fresh = !view || view.projectId !== options.projectId;
    if (fresh) {
      const tab = new URLSearchParams(window.location.search).get("tab");
      view = { ...options, tab: TABS.some((item) => item.key === tab) ? tab : "today", data: null, error: null, counted: null };
    } else {
      Object.assign(view, options);
    }
    view.main = main;
    view.active = true;
    render();
    load();
  }

  async function load() {
    const current = view;
    const tab = current.tab;
    try {
      const data = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/analytics?view=${tab}`);
      if (view !== current || !current.active || current.tab !== tab) return;
      current.data = data;
      current.error = null;
    } catch (error) {
      if (view !== current || !current.active) return;
      current.error = error.message;
    }
    render();
  }

  function render() {
    const main = view.main;
    if (!main.querySelector(".analytics-view")) {
      main.innerHTML = `<section class="product-view workspace-view analytics-view">
        <header class="workspace-header"><h1>Analytics</h1></header>
        <div class="analytics-body"></div>
      </section>`;
    }
    const body = main.querySelector(".analytics-body");
    const data = view.data;
    const state = data?.card?.state;
    if (view.error && !data) body.innerHTML = `<p class="analytics-empty">${escape(view.error)}</p>`;
    else if (!data) body.innerHTML = `<p class="analytics-empty">Reading the report…</p>`;
    else if (state === "unavailable") body.innerHTML = `<p class="analytics-empty">${escape(data.card.message || "Analytics isn't set up on this deployment.")}</p>`;
    else if (state === "not_set_up") body.innerHTML = notSetUp();
    else if (state === "waiting") body.innerHTML = waiting(data);
    else body.innerHTML = report(data);
    bind(body);
  }

  function notSetUp() {
    return `<div class="analytics-empty">
      <p>Set up Tin Analytics on its card in Integrations, and this page shows who visits your site.</p>
      <button class="analytics-link" type="button" data-analytics-integrations>Open Integrations</button>
    </div>`;
  }

  // REP-V2c: no visit yet, so no tabs; the line and where it goes.
  function waiting(data) {
    const card = data.card;
    const domain = host(card.address).replace(/^www\./, "");
    const csp = Object.entries(card.csp || {}).map(([directive, sources]) => `${directive} ${sources.join(" ")}`).join(" · ");
    const wildcard = (card.counted_hosts || []).some((item) => item.startsWith("*."));
    return `<div class="analytics-tabs is-bare">
        <span class="analytics-meta">no visit yet · ${escape(zoneLabel(data.tz))}${card.checked_at ? ` · checked ${escape(clock(card.checked_at))}` : ""}</span>
      </div>
      <div class="analytics-summary">
        <div class="analytics-heading"><span class="analytics-ring" aria-hidden="true"></span><h2>Waiting for the first visit</h2></div>
        <p>Once this line is on every page of ${escape(domain || "your site")}, the first visit shows up here within a minute. Tin checks again each time you open this page.</p>
      </div>
      <div class="analytics-install">
        <div class="analytics-install-row">
          <span class="analytics-install-label">The line</span>
          <code class="analytics-script">${escape(card.script)}</code>
          <button type="button" data-analytics-copy="script">Copy</button>
        </div>
        <div class="analytics-install-row">
          <span class="analytics-install-label">Where it goes</span>
          <span class="analytics-install-main">In the head of every page, which usually means the layout file that every page uses.</span>
          <span class="analytics-install-slot"></span>
        </div>
        ${csp ? `<div class="analytics-install-row">
          <span class="analytics-install-label">If you set a CSP</span>
          <span class="analytics-install-main">Add Tin's analytics address to ${escape(Object.keys(card.csp).join(" and "))}.<code>${escape(csp)}</code></span>
          <button type="button" data-analytics-copy="csp">Copy</button>
        </div>` : ""}
        <div class="analytics-install-row">
          <span class="analytics-install-label">What's counted</span>
          <span class="analytics-install-main">${wildcard ? `${escape(domain)} and every subdomain, such as www.${escape(domain)}` : escape(host(card.address))}<code>preview deployments and localhost aren't counted</code></span>
          <span class="analytics-install-slot"></span>
        </div>
      </div>
      <p class="analytics-notes">site ${escape(card.site_id)}</p>`;
  }

  function tabs(data, meta) {
    return `<div class="analytics-tabs">
      <div class="workflow-sections analytics-sections" role="tablist" aria-label="Period">
        ${TABS.map((tab) => `<button class="workflow-section ${view.tab === tab.key ? "is-active" : ""}" type="button" role="tab" aria-selected="${view.tab === tab.key}" data-analytics-tab="${tab.key}">${tab.label}</button>`).join("")}
      </div>
      <span class="analytics-meta">${escape(meta)}</span>
    </div>`;
  }

  // The change from the period before, as REP-V2 writes it: "+17% on tuesday", "up from 1".
  function change(item, against) {
    if (!item) return "";
    if (typeof item.percent === "number") {
      if (item.percent === 0) return `same as ${against}`;
      return `${item.percent > 0 ? "+" : "−"}${Math.abs(item.percent)}% on ${against}`;
    }
    if (item.now === item.before) return `same as ${against}`;
    return `${item.now > item.before ? "up" : "down"} from ${count(item.before)}`;
  }

  function report(data) {
    const card = data.card;
    const report = data.report;
    const meta = [
      card.state === "quiet" ? "script quiet" : "script on",
      card.reporter_connected ? "reporter on" : "",
      zoneLabel(data.tz),
      report?.built_at ? `updated ${clock(report.built_at)}` : "",
    ].filter(Boolean).join(" · ");
    if (!report) {
      return `${tabs(data, meta)}
        ${needs(data)}
        <p class="analytics-empty">${data.error ? "Tin couldn't read this report just now. It asks again the next time you open this page." : "No report for this period yet."}</p>`;
    }
    const comparison = data.comparison;
    const against = !comparison ? "" : report.period === "week" ? "the week before" : weekday(comparison.start);
    const changes = comparison?.changes || {};
    return `${tabs(data, meta)}
      ${summary(report, comparison)}
      ${needs(data)}
      ${tiles(report, changes, against)}
      ${chart(report)}
      <div class="analytics-pair">${pages(report)}${cameFrom(report)}</div>
      ${requests(report, card)}
      ${(report.notes || []).length ? `<p class="analytics-notes">${escape(report.notes.join(" · "))}</p>` : ""}`;
  }

  function summary(report, comparison) {
    const finished = report.complete === true;
    let title;
    let detail;
    if (report.period === "week") {
      title = "Last 7 days";
      detail = [
        `${dayLabel(report.start)} – ${dayLabel(report.end)}`,
        finished ? "" : "today so far",
        comparison ? `against ${dayLabel(comparison.start)} – ${dayLabel(comparison.end)}` : "",
      ];
    } else {
      const name = weekday(report.start);
      title = `${name.charAt(0).toUpperCase()}${name.slice(1)}${finished ? "" : " so far"}`;
      detail = [
        dayLabel(report.start),
        finished ? "" : `00:00–${clock(report.built_at)}`,
        comparison ? `against ${weekday(comparison.start)}${comparison.until ? " at the same hour" : ""}` : "",
      ];
    }
    return `<div class="analytics-summary">
      <div class="analytics-heading"><h2>${escape(title)}</h2><span>${escape(detail.filter(Boolean).join(" · "))}</span></div>
      <p>${escape(report.summary)}</p>
    </div>`;
  }

  // REP-V2e: the report's own items, with Tin's action where one helps.
  function needs(data) {
    const card = data.card;
    const items = (data.report?.needs_you || []).map((item) => {
      const moved = (card.moved_hosts || [])[0];
      const action = item.code === "uncounted_hosts" && moved
        ? `<button class="analytics-count" type="button" data-analytics-count="${escape(moved.host)}">Count ${escape(moved.host)} instead</button>`
        : item.code === "script_quiet" ? `<button class="analytics-link" type="button" data-analytics-integrations>See the line</button>` : "";
      return { text: item.text, action };
    });
    if (card.state === "needs_attention") {
      items.unshift({
        text: "Tin can no longer read this site's numbers. Set it up again on its card in Integrations.",
        action: `<button class="analytics-link" type="button" data-analytics-integrations>Open Integrations</button>`,
      });
    }
    if (view.counted) {
      return `<div class="analytics-needs"><div class="analytics-needs-row is-done"><span aria-hidden="true"></span><p>Now counting ${escape(view.counted)} and its subdomains. The script line on your site stays the same.</p><code>from the next visit</code></div></div>`;
    }
    if (!items.length) {
      const when = view.tab === "week" ? " this week" : view.tab === "today" ? " today" : "";
      return `<div class="analytics-needs"><div class="analytics-needs-row is-done"><span aria-hidden="true"></span><p class="is-quiet">Nothing needs you${when}</p></div></div>`;
    }
    return `<div class="analytics-needs">${items.map((item) => `<div class="analytics-needs-row"><span aria-hidden="true"></span><p>${escape(item.text)}</p>${item.action}</div>`).join("")}</div>`;
  }

  function tiles(report, changes, against) {
    const totals = report.totals || {};
    const browsers = report.kinds?.automated_browser || {};
    const named = (browsers.named || []).slice(0, 2)
      .map((item) => `${company(item.operator)} ${count(item.visits)} · ${item.evidence === "signature" ? "signed" : "from its network"}`).join(" · ");
    const tile = (label, value, detail) => `<div class="analytics-tile"><span>${escape(label)}</span><strong>${count(value)}</strong><code>${escape(detail || "")}</code></div>`;
    return `<div class="analytics-tiles">
      ${tile("Visits", totals.visits, change(changes.visits, against))}
      ${tile("Page views", totals.page_views, typeof totals.pages_per_visit === "number" ? `${totals.pages_per_visit} a visit` : "")}
      ${tile("Arrived from AI assistants", totals.assistant_visits, change(changes.assistant_visits, against))}
      ${tile("Automated browsers", browsers.visits, named || (browsers.visits ? "none named" : ""))}
    </div>`;
  }

  // Each column stacks declared crawlers on automated browsers on visits with no automation seen.
  function column(parts, total, max) {
    if (!total) return `<span class="analytics-bar is-empty${parts.future ? " is-future" : ""}"></span>`;
    const unit = Math.min(17, Math.floor(118 / Math.max(max, 1)));
    const height = (value) => Math.max(2, Math.round(value * unit));
    return `<span class="analytics-bar">
      ${parts.declared ? `<i class="is-declared" style="height:${height(parts.declared)}px"></i>` : ""}
      ${parts.automated ? `<i class="is-automated" style="height:${height(parts.automated)}px"></i>` : ""}
      ${parts.none ? `<i class="is-none" style="height:${height(parts.none)}px"></i>` : ""}
    </span>`;
  }

  function chart(report) {
    const kinds = report.kinds || {};
    const legend = [
      ["is-none", "no automation seen", kinds.no_automation_seen?.visits],
      ["is-automated", "automated browsers", kinds.automated_browser?.visits],
      ["is-declared", "declared crawlers", kinds.declared_crawler?.visits],
    ].map(([kind, label, value]) => `<span><i class="${kind}" aria-hidden="true"></i>${label} ${count(value)}</span>`).join("");
    let bars;
    let axis;
    if (report.hours) {
      const hours = report.hours;
      const rows = Array.from({ length: 24 }, (_, hour) => ({
        none: hours.no_automation_seen?.[hour] || 0,
        automated: hours.automated_browser?.[hour] || 0,
        declared: hours.declared_crawler?.[hour] || 0,
      }));
      const now = report.complete ? 24 : Number(clock(report.built_at).slice(0, 2));
      const totals = rows.map((row) => row.none + row.automated + row.declared);
      const max = Math.max(...totals);
      bars = `<div class="analytics-bars">${rows.map((row, hour) => column({ ...row, future: hour > now }, totals[hour], max)).join("")}</div>`;
      axis = `<div class="analytics-axis">${["00:00", "06:00", "12:00", "18:00", "24:00"].map((label) => `<span>${label}</span>`).join("")}</div>`;
    } else {
      const rows = (report.days || []).map((row) => ({
        date: row.date,
        visits: row.visits,
        automated: row.automated_browser || 0,
        declared: row.declared_crawler || 0,
        none: Math.max(0, (row.visits || 0) - (row.automated_browser || 0) - (row.declared_crawler || 0)),
      }));
      const max = Math.max(0, ...rows.map((row) => row.visits || 0));
      const last = rows.length - 1;
      bars = `<div class="analytics-days">${rows.map((row, index) => `<div class="analytics-day">
          <div class="analytics-day-bar">${column(row, row.visits, max)}</div>
          <div class="analytics-day-label"><span>${escape(`${weekday(row.date).slice(0, 3)} ${day(row.date)?.getUTCDate() ?? ""}${index === last && !report.complete ? " so far" : ""}`)}</span><strong>${count(row.visits)}</strong></div>
        </div>`).join("")}</div>`;
      axis = "";
    }
    const detector = report.detector;
    const verdicts = detector?.visits || {};
    return `<div class="analytics-chart">
      <div class="analytics-card-head"><h3>${report.hours ? "Hour by hour" : "Day by day"}</h3><div class="analytics-legend">${legend}</div></div>
      ${bars}${axis}
      ${detector ? `<div class="analytics-detector">
        <span>Of the ${count(kinds.no_automation_seen?.visits)} with no automation seen, the detector says</span>
        <code>human ${count(verdicts.human)} · not human ${count(verdicts.nonhuman)} · unknown ${count(verdicts.unknown)} · not scored yet ${count(verdicts.unscored)}</code>
        <i></i>
        ${detector.provisional ? "<code class=\"is-muted\">provisional · still being measured</code>" : ""}
      </div>` : ""}
    </div>`;
  }

  function table(title, meta, columns, rows) {
    return `<div class="analytics-table">
      <div class="analytics-card-head"><h3>${escape(title)}</h3><code>${escape(meta)}</code></div>
      <div class="analytics-row is-head"><span>${escape(columns[0].label)}</span>${columns.slice(1).map((column) => `<span class="analytics-num" style="width:${column.width}px">${escape(column.label)}</span>`).join("")}</div>
      ${rows.join("")}
    </div>`;
  }

  function row(name, values, widths, { tags = "", muted = false } = {}) {
    return `<div class="analytics-row"><span class="analytics-name"><span${muted ? " class=\"is-label\"" : ""}>${escape(name)}</span>${tags}</span>${values.map((value, index) => `<span class="analytics-num${value === null || value === undefined ? " is-withheld" : ""}" style="width:${widths[index]}px">${value === "" ? "" : count(value)}</span>`).join("")}</div>`;
  }

  function pages(report) {
    const landed = new Map((report.landing_pages || []).map((item) => [item.path, item.visits]));
    const widths = [56, 56, 64];
    const rows = (report.top_pages || []).map((item) => row(item.path, [item.visits, item.page_views, landed.has(item.path) ? landed.get(item.path) : ""], widths));
    return table("Pages", "by visits", [{ label: "Page" }, { label: "Visits", width: 56 }, { label: "Views", width: 56 }, { label: "Landed", width: 64 }], rows);
  }

  function cameFrom(report) {
    const rows = (report.came_from || []).map((item) => row(item.host, [item.visits], [56], {
      tags: item.assistant ? "<code class=\"analytics-tag\">AI assistant</code>" : "",
    }));
    if (!rows.length) rows.push(row("No visits followed a link from another site", [""], [56], { muted: true }));
    const regions = report.regions || [];
    if (regions.length) {
      rows.push(`<div class="analytics-row is-regions"><span class="analytics-name"><span class="is-label">Regions, from browser time zones</span></span><code class="analytics-regions">${escape(regions.map((item) => `${item.name} ${count(item.visits)}`).join(" · "))}</code></div>`);
    }
    return table("Came from", "the site that linked here", [{ label: "Site" }, { label: "Visits", width: 56 }], rows);
  }

  // Only with the request reporter: every page request the site's own server code answered.
  function requests(report, card) {
    const requests = report.requests;
    if (!requests) return "";
    const classes = requests.by_class || {};
    const tile = (value, label, wide = false) => `<div class="analytics-class${wide ? " is-wide" : ""}"><strong>${count(value)}</strong><span>${escape(label)}</span></div>`;
    const operators = [...(requests.operators || [])]
      .sort((a, b) => ((b.verified || 0) + (b.declared || 0)) - ((a.verified || 0) + (a.declared || 0)));
    const sum = (items, key) => items.reduce((total, item) => total + (item[key] || 0), 0);
    const shown = operators.slice(0, 3);
    const rest = operators.slice(3);
    const companyRows = shown.map((item) => row(company(item.operator), [item.verified, item.declared], [64, 64], {
      tags: `${ROLES[item.role] ? `<code>${escape(ROLES[item.role])}</code>` : ""}${item.ai ? "<code class=\"analytics-tag\">AI</code>" : ""}`,
    }));
    if (rest.length) companyRows.push(row(`${rest.length} more`, [sum(rest, "verified"), sum(rest, "declared")], [64, 64]));
    const aiPages = [...(requests.ai_pages || [])].sort((a, b) => (b.loads || 0) - (a.loads || 0));
    const loadsBy = {};
    for (const page of aiPages) for (const [operator, loads] of Object.entries(page.by_operator || {})) loadsBy[operator] = (loadsBy[operator] || 0) + (loads || 0);
    const columns = Object.keys(loadsBy).sort((a, b) => loadsBy[b] - loadsBy[a]).slice(0, 3);
    const listed = aiPages.slice(0, 3);
    const cells = (page) => columns.map((operator) => page.by_operator?.[operator] ?? 0);
    const pageRows = listed.map((page) => row(page.path, cells(page), columns.map(() => 72)));
    // Everything else each company loaded: its own total when that covers the pages the report
    // lists, else the listed pages that aren't shown.
    const others = columns.map((operator, index) => {
      const shown = listed.reduce((total, page) => total + cells(page)[index], 0);
      const total = sum(operators.filter((item) => item.operator === operator), "verified")
        + sum(operators.filter((item) => item.operator === operator), "declared");
      return (total >= loadsBy[operator] ? total : loadsBy[operator]) - shown;
    });
    if (others.some((value) => value > 0)) pageRows.push(row("Other pages", others, columns.map(() => 72), { muted: true }));
    const loads = [...listed.flatMap(cells), ...others].reduce((a, b) => a + b, 0);
    return `<div class="analytics-heading is-section"><h2>Every page request</h2><span>from the reporter on your server${card.requests_last_at ? ` · last record ${escape(clock(card.requests_last_at))}` : ""}</span></div>
      <div class="analytics-classes">
        ${tile(requests.page_loads, "page loads")}
        ${tile(classes.verified_bot, "verified bots")}
        ${tile(classes.declared_bot, "named bots, unconfirmed")}
        ${tile(classes.says_bot, "other bots")}
        ${tile(classes.browser_like, `browser-like · script didn't run on ${count(requests.script?.browser_like_not_seen)}`, true)}
      </div>
      <div class="analytics-pair">
        ${table("Companies that asked for pages", `${count(sum(operators, "verified"))} verified · ${count(sum(operators, "declared"))} declared`, [{ label: "Company" }, { label: "Verified", width: 64 }, { label: "Declared", width: 64 }], companyRows)}
        ${columns.length ? table("Pages AI companies loaded", `${count(loads)} loads`, [{ label: "Page" }, ...columns.map((operator) => ({ label: company(operator), width: 72 }))], pageRows) : ""}
      </div>`;
  }

  function bind(body) {
    body.querySelectorAll("[data-analytics-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        if (view.tab === button.dataset.analyticsTab) return;
        view.tab = button.dataset.analyticsTab;
        view.data = view.data && { ...view.data, report: null, comparison: null, error: null };
        const url = new URL(window.location.href);
        if (view.tab === "today") url.searchParams.delete("tab");
        else url.searchParams.set("tab", view.tab);
        window.history.replaceState(null, "", `${url.pathname}${url.search}`);
        render();
        load();
      });
    });
    body.querySelectorAll("[data-analytics-integrations]").forEach((button) => {
      button.addEventListener("click", () => view.openIntegrations());
    });
    body.querySelectorAll("[data-analytics-count]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        const updated = await view.countHost(button.dataset.analyticsCount);
        if (!view.active) return;
        if (updated) {
          view.counted = updated.address ? host(updated.address).replace(/^www\./, "") : button.dataset.analyticsCount;
          view.data = { ...view.data, card: updated };
        }
        render();
      });
    });
    body.querySelectorAll("[data-analytics-copy]").forEach((button) => {
      button.addEventListener("click", async () => {
        const card = view.data.card;
        const text = button.dataset.analyticsCopy === "csp"
          ? Object.entries(card.csp || {}).map(([directive, sources]) => `${directive} ${sources.join(" ")}`).join("\n")
          : card.script;
        try {
          await navigator.clipboard.writeText(text || "");
          view.toast?.("Copied.");
        } catch (_error) {
          view.toast?.("Could not copy. Select it and copy it.");
        }
      });
    });
  }

  function close() {
    if (view) view.active = false;
  }

  return { mount, close };
})();
