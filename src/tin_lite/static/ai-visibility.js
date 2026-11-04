"use strict";

// AI Visibility: how often AI answer engines name and recommend the project, one stored week at
// a time. The week's score, the trend once there are two weeks, and a table of the panel's
// questions by engines: a tick where an engine recommended the project, yellow where it only
// named or cited it, a cross where it did neither. Selecting a cell reads that engine's answers.
// Engines come in two kinds, scored apart: with web search (what people see today) and without
// (what the model already knows). Everything comes from Tin's stored report for the week.
window.TinAIVisibility = (() => {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const KINDS = [
    { key: "web", label: "With web search", note: "What people see today: AI apps as they answer a logged-out user, and models with web search on." },
    { key: "memory", label: "What models already know", note: "The same models with web search off. This moves slowly, months after the web does." },
  ];
  const OUTCOME = {
    recommended: "Recommended",
    mentioned: "Mentioned or cited",
    none: "Not mentioned",
    no_answer: "No answer",
  };
  const DASH = "–";

  let view = null;

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const percent = (rate) => (typeof rate === "number" ? `${Math.round(rate * 1000) / 10}%` : DASH);

  function day(value) {
    const [year, month, date] = String(value || "").split("-").map(Number);
    return year ? new Date(Date.UTC(year, month - 1, date)) : null;
  }
  const short = (date) => `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}`;
  function weekLabel(start) {
    const from = day(start);
    if (!from) return "";
    const to = new Date(from.getTime() + 6 * 86400000);
    return `${short(from)} – ${short(to)}`;
  }

  function mount(main, options) {
    const fresh = !view || view.projectId !== options.projectId;
    if (fresh) {
      const week = new URLSearchParams(window.location.search).get("week");
      view = { ...options, week: /^\d{4}-\d{2}-\d{2}$/.test(week || "") ? week : null, kind: "web", data: null, error: null, open: null, answers: null };
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
    const week = current.week;
    try {
      const query = week ? `?week=${encodeURIComponent(week)}` : "";
      const data = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/ai-visibility${query}`);
      if (view !== current || !current.active || current.week !== week) return;
      current.data = data;
      current.error = null;
    } catch (error) {
      if (view !== current || !current.active) return;
      current.error = error.message;
    }
    render();
  }

  async function loadAnswers(question, engine) {
    const current = view;
    const key = `${current.data.week_start}|${question}|${engine}`;
    current.open = { question, engine, key };
    current.answers = null;
    render();
    try {
      const query = new URLSearchParams({ week: current.data.week_start, question, engine });
      const answers = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/ai-visibility/answers?${query}`);
      if (view !== current || current.open?.key !== key) return;
      current.answers = answers;
    } catch (error) {
      if (view !== current || current.open?.key !== key) return;
      current.answers = { error: error.message };
    }
    render();
  }

  function goToWeek(week) {
    view.week = week;
    view.open = null;
    view.answers = null;
    const url = new URL(window.location.href);
    url.searchParams.set("week", week);
    window.history.replaceState(null, "", url);
    load();
  }

  function render() {
    const main = view.main;
    if (!main.querySelector(".visibility-view")) {
      main.innerHTML = `<section class="product-view workspace-view visibility-view">
        <header class="workspace-header"><h1>AI Visibility</h1><div class="header-spacer"></div><div class="visibility-weeks"></div></header>
        <div class="visibility-body"></div>
      </section>`;
    }
    const body = main.querySelector(".visibility-body");
    const weeks = main.querySelector(".visibility-weeks");
    const data = view.data;
    if (view.error && !data) {
      weeks.innerHTML = "";
      body.innerHTML = `<p class="analytics-empty">${escape(view.error)}</p>`;
    } else if (!data) {
      body.innerHTML = `<p class="analytics-empty">Reading the report…</p>`;
    } else if (!data.week_start) {
      weeks.innerHTML = "";
      body.innerHTML = `<p class="analytics-empty">No AI Visibility report for this project yet.</p>`;
    } else {
      weeks.innerHTML = weekPicker(data);
      body.innerHTML = page(data);
    }
    bind(main);
  }

  function weekPicker(data) {
    const starts = data.weeks.map((item) => item.week_start);
    const index = starts.indexOf(data.week_start);
    const arrow = (direction, target, label) => `<button class="visibility-arrow" type="button" aria-label="${label}"${target ? ` data-visibility-week="${escape(target)}"` : " disabled"}>
        <svg aria-hidden="true" viewBox="0 0 16 16"><path d="${direction === "back" ? "M10 3.5 5.5 8l4.5 4.5" : "M6 3.5 10.5 8 6 12.5"}" /></svg>
      </button>`;
    return `${arrow("back", starts[index - 1], "Previous week")}
      <span class="visibility-week"><strong>${escape(weekLabel(data.week_start))}</strong><small>${index + 1} of ${starts.length} ${starts.length === 1 ? "week" : "weeks"}</small></span>
      ${arrow("next", starts[index + 1], "Next week")}`;
  }

  function page(data) {
    const kind = KINDS.find((item) => item.key === view.kind) || KINDS[0];
    const engines = data.engines.filter((engine) => engine.kind === kind.key);
    const scored = data.questions.filter((question) => question.in_score).length;
    const collected = data.collected_at ? day(data.collected_at.slice(0, 10)) : null;
    const meta = [
      `${scored} questions`,
      `${engines.length} engines`,
      `${data.takes} ${data.takes === 1 ? "answer" : "answers"} each`,
      collected ? `asked ${short(collected)}` : "",
    ].filter(Boolean).join(" · ");
    return `<div class="analytics-tabs">
        <div class="workflow-sections analytics-sections" role="tablist" aria-label="Which answers">
          ${KINDS.map((item) => `<button class="workflow-section ${item.key === kind.key ? "is-active" : ""}" type="button" role="tab" aria-selected="${item.key === kind.key}" data-visibility-kind="${item.key}">${item.label}</button>`).join("")}
        </div>
        <span class="analytics-meta">${escape(meta)}</span>
      </div>
      ${tiles(data, kind.key)}
      ${data.weeks.length > 1 ? trend(data, kind.key) : ""}
      <div class="visibility-key">
        <p>${escape(kind.note)}</p>
        <div class="visibility-legend">
          <span>${mark("recommended")}Recommended</span>
          <span>${mark("mentioned")}Mentioned or cited</span>
          <span>${mark("none")}Not mentioned</span>
          <span><span class="visibility-frac is-key">1/${data.takes}</span>in ${data.takes === 2 ? "one of two answers" : "some of the answers"}</span>
        </div>
      </div>
      ${grid(data, engines, kind.key)}`;
  }

  function tiles(data, kind) {
    const now = data.score[kind];
    const before = data.previous?.[kind];
    const change = (key) => {
      if (!before || typeof now[key] !== "number" || typeof before[key] !== "number") return before ? "" : "first week";
      const points = Math.round((now[key] - before[key]) * 1000) / 10;
      return points === 0 ? "same as the week before" : `${points > 0 ? "+" : "−"}${Math.abs(points)} pts on the week before`;
    };
    const tile = (label, value, detail, cls = "") => `<div class="analytics-tile${cls}"><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(detail || "")}</small></div>`;
    const rival = now.rivals?.[0];
    return `<div class="analytics-tiles visibility-tiles">
      ${tile("Recommended", percent(now.recommended_rate), [`${now.recommended} of ${now.answers} answers`, change("recommended_rate")].filter(Boolean).join(" · "))}
      ${tile("Mentioned or cited", percent(now.mentioned_rate), [`${now.mentioned} of ${now.answers} answers`, change("mentioned_rate")].filter(Boolean).join(" · "))}
      ${tile(`Questions where ${view.brand || "you"} shows up`, `${now.questions_named} of ${now.questions}`, "named in at least one answer")}
      ${tile("Recommended most often instead", rival ? rival.name : DASH, rival ? `in ${rival.answers} of ${now.answers} answers` : "", " is-name")}
    </div>`;
  }

  // Mentioned or cited, and recommended, as a share of the kind's answers each week.
  function trend(data, kind) {
    const weeks = data.weeks;
    const rates = weeks.map((item) => item[kind]);
    const highest = Math.max(0.05, ...rates.map((item) => item.mentioned_rate || 0));
    const top = Math.ceil((highest * 100) / 5) * 5;
    const width = 640;
    const height = 140;
    const x = (index) => 8 + (index * (width - 16)) / Math.max(1, weeks.length - 1);
    const y = (rate) => height - 4 - ((rate || 0) * 100 * (height - 12)) / top;
    const line = (key) => rates.map((item, index) => `${x(index).toFixed(1)},${y(item[key]).toFixed(1)}`).join(" ");
    const dots = (key, cls) => rates.map((item, index) => `<circle class="${cls}" cx="${x(index).toFixed(1)}" cy="${y(item[key]).toFixed(1)}" r="${weeks[index].week_start === data.week_start ? 5 : 3.5}" />`).join("");
    return `<div class="analytics-chart visibility-trend">
      <div class="analytics-card-head"><h3>Week by week</h3>
        <div class="analytics-legend"><span><i class="is-mentioned" aria-hidden="true"></i>mentioned or cited</span><span><i class="is-recommended" aria-hidden="true"></i>recommended</span></div>
      </div>
      <div class="visibility-plot">
        <div class="visibility-scale"><span>${top}%</span><span>${top / 2}%</span><span>0%</span></div>
        <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Mentioned and recommended, week by week">
          <line class="visibility-rule" x1="0" y1="${y(top / 200)}" x2="${width}" y2="${y(top / 200)}" />
          <line class="visibility-rule is-base" x1="0" y1="${height - 4}" x2="${width}" y2="${height - 4}" />
          <polyline class="visibility-line is-mentioned" points="${line("mentioned_rate")}" />
          <polyline class="visibility-line is-recommended" points="${line("recommended_rate")}" />
          ${dots("mentioned_rate", "visibility-dot is-mentioned")}${dots("recommended_rate", "visibility-dot is-recommended")}
        </svg>
      </div>
      <div class="visibility-axis">
        ${weeks.map((item) => `<button type="button" class="${item.week_start === data.week_start ? "is-current" : ""}" data-visibility-week="${escape(item.week_start)}" aria-label="Week of ${escape(weekLabel(item.week_start))}: mentioned ${percent(item[kind].mentioned_rate)}, recommended ${percent(item[kind].recommended_rate)}">${escape(short(day(item.week_start)))}</button>`).join("")}
      </div>
    </div>`;
  }

  function mark(outcome) {
    const glyph = {
      recommended: '<svg aria-hidden="true" viewBox="0 0 16 16"><path d="M3.5 8.5 6.5 11.5 12.5 4.5" /></svg>',
      mentioned: '<svg aria-hidden="true" viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.5" /></svg>',
      none: '<svg aria-hidden="true" viewBox="0 0 16 16"><path d="M5 5l6 6M11 5l-6 6" /></svg>',
      no_answer: '<span aria-hidden="true">–</span>',
    }[outcome];
    return `<span class="visibility-mark is-${outcome}">${glyph}</span>`;
  }

  // Two letters for an engine's mark: the first and last word's initials ("Gemini 4 Argon" GA,
  // "GPT-6.1 Sol" GS), or a one-word name's first two letters ("ChatGPT" CH).
  function monogram(engine) {
    const words = engine.name.split(/\s+/).filter((word) => /[A-Za-z]/.test(word));
    if (!words.length) return "";
    return (words.length > 1 ? words[0][0] + words.at(-1)[0] : words[0].slice(0, 2)).toUpperCase();
  }

  function grid(data, engines, kind) {
    const columns = `minmax(240px, 320px) repeat(${engines.length}, 60px) minmax(160px, 1fr)`;
    const head = `<div class="visibility-row is-head" style="grid-template-columns:${columns}">
        <span class="visibility-question">Question</span>
        ${engines.map((engine) => `<span class="visibility-engine" title="${escape(`${engine.name}${engine.detail ? `, ${engine.detail}` : ""}`)}"><i aria-hidden="true">${escape(monogram(engine))}</i><strong>${escape(engine.name)}</strong><small>${escape(engine.detail)}</small></span>`).join("")}
        <span class="visibility-rivals">Recommended instead</span>
      </div>`;
    const rows = [];
    let section = null;
    for (const group of data.groups) {
      const questions = data.questions.filter((question) => question.group === group.key);
      if (!questions.length) continue;
      if (group.section !== section) {
        section = group.section;
        const title = data.sections.find((item) => item.key === section)?.title || "";
        const count = data.questions.filter((question) => data.groups.find((item) => item.key === question.group)?.section === section).length;
        rows.push(`<div class="visibility-band"><strong>${escape(title)}</strong><small>${count} ${count === 1 ? "question" : "questions"}</small></div>`);
      }
      rows.push(`<div class="visibility-group"><strong>${escape(group.title)}</strong>${group.note ? `<small>${escape(group.note)}</small>` : ""}</div>`);
      for (const question of questions) {
        const cells = data.cells[question.id] || {};
        const open = view.open?.question === question.id && engines.some((engine) => engine.key === view.open.engine);
        rows.push(`<div class="visibility-row" style="grid-template-columns:${columns}">
            <span class="visibility-question"><small>${escape(question.id)}</small>${escape(question.text)}</span>
            ${engines.map((engine) => cell(question, engine, cells[engine.key])).join("")}
            <span class="visibility-rivals">${escape((data.rivals[question.id]?.[kind] || []).join(", ") || DASH)}</span>
          </div>`);
        if (open) rows.push(detail());
      }
    }
    return `<div class="visibility-table"><div class="visibility-scroll"><div class="visibility-grid" style="min-width:${320 + engines.length * 60 + 160}px">${head}${rows.join("")}</div></div></div>
      <p class="analytics-notes">Each engine is asked every question ${data.takes === 1 ? "once" : `${data.takes} times`} a week. A cell shows the best of its answers; select it to read what the engine said, what it searched for and which pages it cited.</p>`;
  }

  function cell(question, engine, value) {
    if (!value) return `<span class="visibility-cell">${mark("no_answer")}</span>`;
    const selected = view.open?.question === question.id && view.open?.engine === engine.key;
    const partial = ["recommended", "mentioned"].includes(value.outcome) && value.count < value.of;
    const label = `${engine.name}: ${OUTCOME[value.outcome]}${partial ? ` in ${value.count} of ${value.of} answers` : ""}. Show the answers.`;
    return `<span class="visibility-cell"><button type="button" class="visibility-button${selected ? " is-selected" : ""}" data-visibility-cell="${escape(question.id)}" data-visibility-engine="${escape(engine.key)}" aria-label="${escape(label)}" aria-expanded="${selected}">
        ${mark(value.outcome)}${partial ? `<span class="visibility-frac">${value.count}/${value.of}</span>` : ""}
      </button></span>`;
  }

  function detail() {
    const answers = view.answers;
    if (!answers) return `<div class="visibility-detail"><p class="analytics-empty">Reading the answers…</p></div>`;
    if (answers.error) return `<div class="visibility-detail"><p class="analytics-empty">${escape(answers.error)}</p></div>`;
    const engine = answers.engine;
    const takes = answers.answers.map((answer) => {

      return `<div class="visibility-answer">
        <div class="visibility-answer-head">${mark(answer.outcome)}<strong>Answer ${answer.take}: ${escape(OUTCOME[answer.outcome])}</strong></div>
        ${answer.quote ? `<blockquote>${escape(answer.quote)}</blockquote>` : ""}
        <div class="visibility-facts">
          <div><h4>What it searched for</h4>${answer.queries.length ? `<ul>${answer.queries.slice(0, 8).map((query) => `<li><code>${escape(query)}</code></li>`).join("")}</ul>` : `<p>${engine.kind === "memory" ? "Web search was off." : "This engine doesn't show its searches, or didn't search."}</p>`}</div>
          <div>${answer.cited.length ? sources("Sources it cited", answer.cited, 8) : sources("Pages it read", answer.read, 8)}</div>
          <div><h4>Recommended instead</h4><p>${escape(answer.recommended_instead.slice(0, 6).join(", ") || DASH)}</p></div>
        </div>
      </div>`;
    }).join("");
    return `<div class="visibility-detail">
      <div class="visibility-detail-head"><strong>${escape(engine.name)}${engine.detail ? ` · ${escape(engine.detail)}` : ""}</strong><button type="button" class="visibility-close" data-visibility-close>Close</button></div>
      ${takes || '<p class="analytics-empty">No answers for this cell.</p>'}
    </div>`;
  }

  // A source as its address without the scheme, linked when it is a web address; the same page
  // cited twice is listed once.
  function sources(title, all, limit) {
    const seen = new Set();
    const pages = [];
    for (const source of all) {
      const shown = place(source);
      if (seen.has(shown.text)) continue;
      seen.add(shown.text);
      pages.push({ ...shown, domain: source.domain });
    }
    if (!pages.length) return `<h4>Sources it cited</h4><p>None.</p>`;
    const more = pages.length - limit;
    return `<h4>${title}</h4><ul>${pages.slice(0, limit).map((page) => `<li${yours(page.domain) ? ' class="is-yours"' : ""}>${page.href ? `<a href="${escape(page.href)}" target="_blank" rel="noopener noreferrer">${escape(page.text)}</a>` : escape(page.text)}</li>`).join("")}</ul>${more > 0 ? `<p>and ${more} more</p>` : ""}`;
  }

  function place(source) {
    try {
      const url = new URL(source.url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("not a web address");
      // Google's grounding links redirect; their real site is the domain the report names.
      if (url.hostname.endsWith("vertexaisearch.cloud.google.com")) return { text: source.domain || url.hostname, href: null };
      const path = url.pathname === "/" ? "" : url.pathname.replace(/\/$/, "");
      return { text: `${url.hostname.replace(/^www\./, "")}${path}`, href: url.href };
    } catch (_error) {
      return { text: source.domain || source.url, href: null };
    }
  }

  // The project's own site, so its pages stand out among the cited sources.
  function yours(domain) {
    const own = view.data?.domain;
    return Boolean(own && domain && (domain === own || domain.endsWith(`.${own}`)));
  }

  function bind(main) {
    main.querySelectorAll("[data-visibility-week]").forEach((button) => button.addEventListener("click", () => goToWeek(button.dataset.visibilityWeek)));
    main.querySelectorAll("[data-visibility-kind]").forEach((button) => button.addEventListener("click", () => {
      view.kind = button.dataset.visibilityKind;
      view.open = null;
      view.answers = null;
      render();
    }));
    main.querySelectorAll("[data-visibility-cell]").forEach((button) => button.addEventListener("click", () => {
      const { visibilityCell: question, visibilityEngine: engine } = button.dataset;
      if (view.open?.question === question && view.open?.engine === engine) {
        view.open = null;
        view.answers = null;
        render();
      } else {
        loadAnswers(question, engine);
      }
    }));
    main.querySelector("[data-visibility-close]")?.addEventListener("click", () => {
      const { question, engine } = view.open || {};
      view.open = null;
      view.answers = null;
      render();
      main.querySelector(`[data-visibility-cell="${CSS.escape(question || "")}"][data-visibility-engine="${CSS.escape(engine || "")}"]`)?.focus();
    });
  }

  function close() {
    if (view) view.active = false;
  }

  return { mount, close };
})();
