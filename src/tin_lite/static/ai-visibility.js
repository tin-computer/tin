"use strict";

// AI Visibility: how often AI answer engines name and recommend the project, one stored week at
// a time. The week's score, the trend once there are two weeks, and a table of the panel's
// questions by engines: a tick where an engine recommended the project, yellow where it only
// named or cited it, a cross where it did neither. Selecting a cell reads that engine's answers.
// Design: Paper "AI Visibility · dashboard (from code)", boards AV-A and AV-A round two (M1): the
// Analytics page's summary sentence and tiles, the Recommended tile washed moss or crimson when
// the week moved by SIGNIFICANT points or more, and the page's chips in the palette.
// The page shows the engines that answer with web search, which is what people see today; the
// report also holds models asked without it, which the page leaves out for now. Questions kept
// out of the score (citation probes, honesty checks, branded comparisons) sit in their own table
// after the main one. Everything comes from Tin's stored report for the week.
window.TinAIVisibility = (() => {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  // A week-to-week move smaller than this many points stays uncoloured: at a few hundred answers
  // a week, the standard error of the difference between two weeks is about 2.5 points.
  const SIGNIFICANT = 3;
  // The engine kind the page shows: answers with web search.
  const KIND = "web";
  const OUTCOME = {
    recommended: "Recommended",
    mentioned: "Mentioned or cited",
    none: "Not mentioned",
    no_answer: "No answer",
  };
  const DASH = "–";
  // Marks shipped in /assets/ai-engines/; any other engine shows its initials.
  const ICONS = new Set(["openai", "claude", "gemini", "google", "perplexity", "kimi", "zai"]);
  // Single-colour marks are drawn in ink, so they follow the theme like Integrations' GitHub and X.
  // Kimi's K is currentColor, which an <img> renders black, so it goes in ink too.
  const MONOCHROME = new Set(["openai", "zai", "kimi"]);

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
    return `${short(from)} – ${to.getUTCMonth() === from.getUTCMonth() ? to.getUTCDate() : short(to)}`;
  }

  function mount(main, options) {
    const fresh = !view || view.projectId !== options.projectId;
    if (fresh) {
      const week = new URLSearchParams(window.location.search).get("week");
      view = { ...options, week: /^\d{4}-\d{2}-\d{2}$/.test(week || "") ? week : null, data: null, error: null, open: null, answers: null, unfolded: new Set() };
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
    current.unfolded = new Set();
    current.take = null;
    render();
    try {
      const query = new URLSearchParams({ week: current.data.week_start, question, engine });
      const answers = await current.api(`/api/projects/${encodeURIComponent(current.projectId)}/ai-visibility/answers?${query}`);
      if (view !== current || current.open?.key !== key) return;
      current.answers = answers;
      // Open on the answer that went furthest for the project.
      const rank = { recommended: 3, mentioned: 2, none: 1, no_answer: 0 };
      current.take = answers.answers.reduce((best, item) => (!best || rank[item.outcome] > rank[best.outcome] ? item : best), null)?.take ?? null;
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
      <span class="visibility-week"><strong>${escape(weekLabel(data.week_start))}</strong></span>
      ${arrow("next", starts[index + 1], "Next week")}`;
  }

  function page(data) {
    const engines = data.engines.filter((engine) => engine.kind === KIND);
    return `${summary(data, engines)}
      ${tiles(data, KIND)}
      ${data.weeks.length > 1 ? trend(data, KIND) : ""}
      <div class="visibility-key">
        <div class="visibility-legend">
          <span>${mark("recommended")}Recommended</span>
          <span>${mark("mentioned")}Mentioned or cited</span>
          <span><span class="visibility-key-chip">${mark("recommended")}<span class="visibility-frac">1/${data.takes}</span></span>In ${data.takes === 2 ? "one of two answers" : "some of the answers"}</span>
          <span>${mark("none")}Not mentioned</span>
          <span>${mark("no_answer")}No answer</span>
        </div>
      </div>
      ${tables(data, engines, KIND)}`;
  }

  // The week in a sentence, the way Analytics opens a day.
  function summary(data, engines) {
    const now = data.score[KIND];
    const brand = view.brand || "you";
    const it = view.brand ? "it" : "you";
    const start = day(data.week_start);
    const each = data.takes === 1 ? "one answer each" : data.takes === 2 ? "two answers each" : `${data.takes} answers each`;
    const facts = `${now.questions} ${now.questions === 1 ? "question" : "questions"}, ${engines.length} ${engines.length === 1 ? "engine" : "engines"}, ${each}`;
    const rival = now.rivals?.[0];
    const reach = now.questions_named === now.questions
      ? `Every question brought ${it} up at least once.`
      : now.questions_named
        ? `Of the ${now.questions} questions, ${now.questions_named} brought ${it} up at least once.`
        : `None of the ${now.questions} questions brought ${it} up.`;
    const sentence = now.answers
      ? [
          `AI engines recommended ${brand} in ${now.recommended} of ${now.answers} answers and named or cited ${it} in ${now.mentioned}.`,
          reach,
          rival ? `${rival.name} was recommended instead most often, in ${rival.answers} answers.` : "",
        ].filter(Boolean).join(" ")
      : "No engine answered this week's questions.";
    return `<div class="analytics-summary visibility-summary">
      <div class="analytics-heading"><h2>Week of ${escape(start ? `${FULL_MONTHS[start.getUTCMonth()]} ${start.getUTCDate()}` : "")}</h2><span>${escape(facts)}</span></div>
      <p>${escape(sentence)}</p>
    </div>`;
  }

  // Four numbers, Recommended first. Its tile turns moss when the share rose by SIGNIFICANT
  // points or more on the week before and crimson when it fell as far; a smaller move or a first
  // week leaves it plain.
  function tiles(data, kind) {
    const now = data.score[kind];
    const before = data.previous?.[kind];
    const index = data.weeks.findIndex((item) => item.week_start === data.week_start);
    const last = index > 0 ? weekLabel(data.weeks[index - 1].week_start) : "the week before";
    const move = (key) => (before && typeof now[key] === "number" && typeof before[key] === "number"
      ? Math.round((now[key] - before[key]) * 1000) / 10
      : null);
    const said = (points) => {
      if (points === null) return "";
      if (points === 0) return "Same as the week before";
      const text = `${points > 0 ? "+" : "−"}${Math.abs(points)} points`;
      return Math.abs(points) < SIGNIFICANT ? `${text}, about the same` : `${text} on ${last}`;
    };
    const recommended = move("recommended_rate");
    const state = recommended === null || Math.abs(recommended) < SIGNIFICANT ? "" : recommended > 0 ? " is-up" : " is-down";
    const tile = (label, value, detail, change, cls = "") => `<div class="analytics-tile${cls}">
        <span>${escape(label)}</span><strong>${escape(value)}</strong>
        <small>${escape(detail)}${change ? `<span class="visibility-delta">${escape(change)}</span>` : ""}</small>
      </div>`;
    const rival = now.rivals?.[0];
    return `<div class="analytics-tiles visibility-tiles">
      ${tile("Recommended", percent(now.recommended_rate), `${now.recommended} of ${now.answers} answers`, before ? said(recommended) : "First week", ` is-recommended${state}`)}
      ${tile("Mentioned or cited", percent(now.mentioned_rate), `${now.mentioned} of ${now.answers} answers`, said(move("mentioned_rate")))}
      ${tile(`Questions that bring ${view.brand ? "it" : "you"} up`, `${now.questions_named} of ${now.questions}`, "in at least one answer", "")}
      ${tile("Recommended instead most", rival ? rival.name : DASH, rival ? `in ${rival.answers} of ${now.answers} answers` : "", "", rival && rival.name.length > 14 ? " is-name" : "")}
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

  function logo(engine) {
    if (!ICONS.has(engine.icon)) return `<i class="visibility-logo" aria-hidden="true">${escape(monogram(engine))}</i>`;
    const path = `/assets/ai-engines/${engine.icon}.svg`;
    return MONOCHROME.has(engine.icon)
      ? `<i class="visibility-logo is-monochrome" style="--logo-mask: url('${path}')" aria-hidden="true"><img src="${path}" alt="" /></i>`
      : `<i class="visibility-logo" aria-hidden="true"><img src="${path}" alt="" /></i>`;
  }

  // Two letters for an engine's mark: the first and last word's initials ("Gemini 4 Argon" GA,
  // "GPT-6.1 Sol" GS), or a one-word name's first two letters ("ChatGPT" CH).
  function monogram(engine) {
    const words = engine.name.split(/\s+/).filter((word) => /[A-Za-z]/.test(word));
    if (!words.length) return "";
    return (words.length > 1 ? words[0][0] + words.at(-1)[0] : words[0].slice(0, 2)).toUpperCase();
  }

  // The scored questions in one table; the ones kept out of the score in a second, after a
  // break, so they never read as part of the result.
  function tables(data, engines, kind) {
    const scored = data.groups.filter((group) => data.questions.some((question) => question.group === group.key && question.in_score));
    const apart = data.groups.filter((group) => !scored.includes(group) && data.questions.some((question) => question.group === group.key));
    return `${grid(data, engines, kind, scored, true)}
      ${apart.length ? `<section class="visibility-apart" aria-label="Not counted in the score">
        <div class="visibility-apart-head">
          <h2>Not counted in the score</h2>
          <p>These questions test something else: whether engines cite your site, whether they recommend you for something you don't offer, and how they compare you when asked by name.</p>
        </div>
        ${grid(data, engines, kind, apart, false)}
      </section>` : ""}`;
  }

  function grid(data, engines, kind, groups, bands) {
    // Ten engines still fit the dashboard's width with slightly narrower columns.
    const width = engines.length > 8 ? 58 : 64;
    const question = engines.length > 8 ? 270 : 300;
    const columns = `minmax(min(${question}px, 50vw), 1fr) repeat(${engines.length}, ${width}px) minmax(160px, 200px)`;
    const head = `<div class="visibility-row is-head" style="grid-template-columns:${columns}">
        <span class="visibility-question">Question</span>
        ${engines.map((engine) => `<span class="visibility-engine" title="${escape(`${engine.name}${engine.detail ? `, ${engine.detail}` : ""}`)}">${logo(engine)}<strong>${escape(engine.name)}</strong></span>`).join("")}
        <span class="visibility-rivals">Recommended instead</span>
      </div>`;
    const rows = [];
    let section = null;
    for (const group of groups) {
      const questions = data.questions.filter((question) => question.group === group.key);
      if (bands && group.section !== section) {
        section = group.section;
        const title = data.sections.find((item) => item.key === section)?.title || "";
        const count = data.questions.filter((question) => question.in_score && data.groups.find((item) => item.key === question.group)?.section === section).length;
        rows.push(`<div class="visibility-band"><strong>${escape(title)}</strong><small>${count} ${count === 1 ? "question" : "questions"}</small></div>`);
      }
      rows.push(`<div class="visibility-group"><strong>${escape(group.title)}</strong>${group.note ? `<small>${escape(group.note)}</small>` : ""}</div>`);
      for (const question of questions) {
        const cells = data.cells[question.id] || {};
        const open = view.open?.question === question.id && engines.some((engine) => engine.key === view.open.engine);
        rows.push(`<div class="visibility-row" style="grid-template-columns:${columns}">
            <span class="visibility-question" title="${escape(question.text)}"><span>${escape(question.text)}</span></span>
            ${engines.map((engine) => cell(question, engine, cells[engine.key])).join("")}
            <span class="visibility-rivals"><span>${escape((data.rivals[question.id]?.[kind] || []).join(", ") || DASH)}</span></span>
          </div>`);
        if (open) rows.push(detail());
      }
    }
    return `<div class="visibility-table"><div class="visibility-scroll"><div class="visibility-grid" style="min-width:calc(min(${question}px, 50vw) + ${engines.length * width + 160}px)">${head}${rows.join("")}</div></div></div>`;
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
    const answer = answers.answers.find((item) => item.take === view.take) || answers.answers[0];
    const tabs = answers.answers.length > 1
      ? `<div class="visibility-takes" role="tablist" aria-label="Answers">${answers.answers.map((item) => `<button type="button" role="tab" class="visibility-take${item === answer ? " is-active" : ""}" aria-selected="${item === answer}" data-visibility-take="${item.take}">${mark(item.outcome)}<span>Answer ${item.take}</span></button>`).join("")}</div>`
      : "";
    return `<div class="visibility-detail">
      <div class="visibility-detail-head">
        <span class="visibility-detail-title">${logo(engine)}<strong>${escape(engine.name)}</strong>${engine.detail ? `<small>${escape(engine.detail)}</small>` : ""}</span>
        ${tabs}
        <button type="button" class="visibility-close" data-visibility-close>Close</button>
      </div>
      <p class="visibility-asked"><small>${escape(answers.question.id)}</small>${escape(answers.question.text)}</p>
      ${answer ? one(answer, engine) : '<p class="analytics-empty">No answers for this cell.</p>'}
    </div>`;
  }

  function one(answer, engine) {
    const facts = [
      answer.queries.length ? `${answer.queries.length} ${answer.queries.length === 1 ? "search" : "searches"}` : "",
      answer.cited.length ? `${answer.cited.length} cited` : answer.read.length ? `${answer.read.length} pages read` : "",
    ].filter(Boolean).join(" · ");
    const searched = answer.queries.length
      ? `<ul class="visibility-queries">${answer.queries.slice(0, 6).map((query) => `<li><code>${escape(query)}</code></li>`).join("")}</ul>${answer.queries.length > 6 ? `<p class="visibility-more">and ${answer.queries.length - 6} more</p>` : ""}`
      : `<p class="visibility-quiet">${engine.kind === "memory" ? "Web search was off." : "This engine doesn't show its searches, or didn't search."}</p>`;
    return `<div class="visibility-answer" role="tabpanel">
        <div class="visibility-answer-head"><strong>${escape(OUTCOME[answer.outcome])}</strong>${facts ? `<small>${escape(facts)}</small>` : ""}</div>
        ${answer.quote ? `<blockquote class="visibility-quote">${inline(answer.quote.replace(/\s+/g, " "))}</blockquote>` : ""}
        ${fullAnswer(answer)}
        <div class="visibility-facts">
          <section><h4>What it searched for</h4>${searched}</section>
          <section>${answer.cited.length ? sources("Sources it cited", answer.cited, 6) : sources("Pages it read", answer.read, 6)}</section>
          <section><h4>Recommended instead</h4>${answer.recommended_instead.length ? `<ul class="visibility-names">${answer.recommended_instead.slice(0, 8).map((name) => `<li>${escape(name)}</li>`).join("")}</ul>` : `<p class="visibility-quiet">None named.</p>`}</section>
        </div>
      </div>`;
  }

  // The engine's whole answer, folded to its first line until the founder opens it.
  function fullAnswer(answer) {
    const text = String(answer.text || "").trim();
    if (!text) return "";
    const open = view.unfolded.has(answer.take);
    const first = plain(text.split("\n").find((line) => plain(line)) || "");
    const id = `visibility-answer-${answer.take}`;
    return `<div class="visibility-full${open ? " is-open" : ""}">
        <button type="button" class="visibility-fold" data-visibility-fold="${answer.take}" aria-expanded="${open}" aria-controls="${id}">
          <svg aria-hidden="true" viewBox="0 0 16 16"><path d="${open ? "M3.5 10 8 5.5l4.5 4.5" : "M3.5 6 8 10.5 12.5 6"}" /></svg>
          ${open ? "Hide the full answer" : "Read the full answer"}
        </button>
        ${open
          ? `<div class="visibility-text" id="${id}">${markdown(text)}</div>`
          : `<p class="visibility-first" id="${id}">${escape(first)}…</p>`}
      </div>`;
  }

  // A line as plain words: Markdown marks and links' addresses dropped.
  function plain(line) {
    return line.replace(/^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*|\|)/, "")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*_`|]+/g, "").replace(/\s+/g, " ").trim();
  }

  // A small Markdown reader for answers: headings, lists, quotes, tables, paragraphs, bold,
  // italics, code and http(s) links. Every piece of text is escaped first, so nothing in an
  // answer becomes markup of its own.
  function markdown(source) {
    const lines = source.replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let list = null;
    let paragraph = [];
    const flushParagraph = () => {
      if (paragraph.length) out.push(`<p>${paragraph.map(inline).join("<br />")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>${list.items.map((item) => `<li>${inline(item)}</li>`).join("")}</${list.tag}>`);
      list = null;
    };
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
      const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (!line.trim()) { flushParagraph(); flushList(); continue; }
      if (/^\s*```/.test(line)) {
        flushParagraph(); flushList();
        const code = [];
        for (index += 1; index < lines.length && !/^\s*```/.test(lines[index]); index += 1) code.push(lines[index]);
        out.push(`<pre><code>${escape(code.join("\n"))}</code></pre>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line)) {
        flushParagraph(); flushList();
        const rows = [];
        for (; index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index]); index += 1) rows.push(lines[index]);
        index -= 1;
        const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
        const body = rows.filter((row) => !/^\s*\|[\s:|-]+\|\s*$/.test(row));
        const [head, ...rest] = body;
        out.push(`<div class="visibility-text-table"><table>${head ? `<thead><tr>${cells(head).map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead>` : ""}<tbody>${rest.map((row) => `<tr>${cells(row).map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      if (heading) { flushParagraph(); flushList(); out.push(`<h5>${inline(heading[2])}</h5>`); continue; }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushParagraph(); flushList(); out.push("<hr />"); continue; }
      if (bullet || numbered) {
        flushParagraph();
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push((bullet || numbered)[1]);
        continue;
      }
      if (/^\s*>/.test(line)) { flushParagraph(); flushList(); out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`); continue; }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += ` ${line.trim()}`; continue; }
      flushList();
      paragraph.push(line.trim());
    }
    flushParagraph();
    flushList();
    return out.join("");
  }

  function inline(text) {
    const links = [];
    let html = escape(text).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, href) => {
      links.push(`<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return `\u0000${links.length - 1}\u0000`;
    });
    html = html
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    return html.replace(/\u0000(\d+)\u0000/g, (_match, index) => links[Number(index)]);
  }

  function sources(title, all, limit) {
    const seen = new Set();
    const pages = [];
    for (const source of all) {
      const shown = place(source);
      if (seen.has(shown.text)) continue;
      seen.add(shown.text);
      pages.push({ ...shown, domain: source.domain });
    }
    if (!pages.length) return `<h4>Sources it cited</h4><p class="visibility-quiet">None.</p>`;
    const more = pages.length - limit;
    return `<h4>${title}</h4><ul>${pages.slice(0, limit).map((page) => `<li${yours(page.domain) ? ' class="is-yours"' : ""}>${page.href ? `<a href="${escape(page.href)}" target="_blank" rel="noopener noreferrer">${escape(page.text)}</a>` : escape(page.text)}</li>`).join("")}</ul>${more > 0 ? `<p class="visibility-more">and ${more} more</p>` : ""}`;
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
    main.querySelectorAll("[data-visibility-take]").forEach((button) => button.addEventListener("click", () => {
      view.take = Number(button.dataset.visibilityTake);
      render();
      main.querySelector(`[data-visibility-take="${view.take}"]`)?.focus();
    }));
    main.querySelectorAll("[data-visibility-fold]").forEach((button) => button.addEventListener("click", () => {
      const take = Number(button.dataset.visibilityFold);
      if (view.unfolded.has(take)) view.unfolded.delete(take);
      else view.unfolded.add(take);
      render();
      main.querySelector(`[data-visibility-fold="${take}"]`)?.focus();
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
