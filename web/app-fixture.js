// Packaged dashboard with synthetic APIs, shared by the Files and narrow-layout browser tests.
// No live credentials or projects.
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";

const assets = path.resolve("src/tin_lite/static");
export const revision = "c".repeat(40);

export const projectFiles = [
  "brand/BRAND.md",
  "brand/DESIGN.md",
  "brand/logo.png",
  "brand/proposals/2026-09-28-1a2b3c4d/BRAND.md",
  "brand/proposals/2026-09-28-1a2b3c4d/DESIGN.md",
  "context/positioning.md",
  "diagrams/flow.mmd",
  "notes.txt",
  "reports/AI_VISIBILITY.md",
  "reports/answers/page-1.md",
  "reports/keyword-plan/PLAN.md",
  "reports/keyword-plan/keywords.csv",
  "reports/keyword-plan/keywords.json",
  "wiki/INDEX.md",
];

const paragraph = "Founders who use coding agents want marketing tools that read the repository, run on a schedule and open pull requests.";
const documentHtml = [
  '<h1 id="title">Which marketing tools work with coding agents?</h1>',
  ...Array.from({length: 4}, () => `<p>${paragraph}</p>`),
  '<h2 id="short">The short answer</h2>',
  ...Array.from({length: 4}, () => `<p>${paragraph}</p>`),
  '<h2 id="compare">How they compare</h2>',
  ...Array.from({length: 4}, () => `<p>${paragraph}</p>`),
  '<h2 id="questions">Questions</h2>',
  ...Array.from({length: 4}, () => `<p>${paragraph}</p>`),
].join("");

export const profiles = ["ICP 1: founders who need growth done", "ICP 2: technical founders running their own marketing"];

// People as the People API returns them: a fit, an old score, someone not scored, and a note.
export function syntheticPeople() {
  const at = "2026-10-15T09:00:00Z";
  const person = (id, name, headline, icp, fit, relationship, {stale = false, reason = "Runs a small B2B team with no marketing lead.", notes = [], touch = {kind: "reacted", at: "2026-10-13T08:00:00Z", who: "you"}} = {}) => ({
    id, name, headline, profile_url: `https://www.linkedin.com/in/${id}`,
    fields: {
      icp: {value: icp, reason, by: "engagers", at, stale, stale_reason: stale ? "definition" : null, definition: "strategy/ICP.md", previous: null},
      icp_fit: {value: fit, reason, by: "engagers", at, stale, stale_reason: stale ? "definition" : null, definition: "strategy/ICP.md", previous: null},
      linkedin_relationship: {value: relationship, reason: "", by: "engagers", at, stale: false, stale_reason: null, definition: "", previous: null},
    },
    // Who was behind it, for the viewer: "you" or a teammate's first name.
    last_touch: touch,
    added: {at, by: "engagers"},
    notes,
    // Whether the viewer can ask them to connect, and a request of theirs waiting to go out.
    requestable: relationship === "none",
    request: null,
  });
  return [
    person("p-maya", "Maya Lindqvist", "Founder, payroll for remote teams", "ICP 1: founders who need growth done", 0.84, "connected"),
    person("p-daniel", "Daniel Okafor", "Co-founder, Fieldline", "ICP 2: technical founders running their own marketing", 0.78, "none", {notes: [
      {id: "n-daniel-2", body: "Interned with us in 2024 before founding Fieldline.\nA friend of the team.", by: "you", mine: true, at: "2026-10-15T10:00:00Z", edited: false},
      {id: "n-daniel-1", body: "Met at a founder dinner in Berlin.", by: "a teammate", who: "Mira", mine: false, at: "2026-10-14T10:00:00Z", edited: false},
    ], touch: {kind: "invited", at: "2026-10-16T08:00:00Z", who: "Mira"}}),
    person("p-sofia", "Sofia Marchetti", "Founder & CEO, Patchbay", "ICP 1: founders who need growth done", 0.66, "connected", {stale: true}),
    person("p-lea", "Lea Fischer", "Head of Marketing, Quanta Labs", "none", 0.15, "none"),
    person("p-hannah", "Hannah Kim", "Building something new", null, null, "none", {reason: "Tin couldn't score this person this time; the next run tries again.", touch: {kind: "viewed", at: "2026-10-17T08:00:00Z", who: "Mira"}}),
  ];
}

// Someone a visitor journeys run named: recognized by the email they signed up with, no LinkedIn.
export function visitorPerson() {
  const at = "2026-10-16T14:00:00Z";
  const by = {by: "visitors", at, stale: false, stale_reason: null, definition: "", previous: null};
  return {
    id: "p-ana", name: "Ana Souza", headline: "", profile_url: "", email: "ana@fieldnotes.dev",
    fields: {
      web_tier: {value: "new_user", reason: "/scan/connect", ...by},
      web_summary: {value: "Signed up from the hackathon page, picked a plan, stopped at Connect GitHub.", reason: "", ...by},
    },
    last_touch: {kind: "visited", at: "2026-10-15T13:20:00Z"},
    added: {at, by: "visitors"},
    notes: [],
    events: [{
      kind: "visited", at: "2026-10-15T13:20:00Z", tier: "new_user",
      summary: "Signed up from the hackathon page, picked a plan, stopped at Connect GitHub.",
      stopped_at: "/scan/connect", action: "Email her a two-line guide to connecting GitHub.",
      replay_url: "https://us.posthog.com/project/1/replay/01a1449a-9164-7200-8316-b1ea9c8c006a",
      post_url: "", yours: null,
    }],
  };
}

// The list carries each person's newest note; one person carries every note and the timeline.
function listed({notes, ...person}) {
  return {
    ...person,
    note: notes[0] ? {body: notes[0].body, by: notes[0].by, at: notes[0].at, count: notes.length} : null,
    notes_text: notes.map(note => note.body).join(" \n"),
  };
}

function detail(person) {
  return {...person, events: person.events || [
    {kind: "viewed", at: "2026-10-18T08:00:00Z", reaction: null, post_url: "", yours: true, who: "you"},
    {kind: "invited", at: "2026-10-16T08:00:00Z", reaction: null, post_url: "", yours: false, who: "Mira"},
    {kind: "reacted", at: "2026-10-13T08:00:00Z", reaction: "INTEREST", post_url: "https://www.linkedin.com/feed/update/urn:li:activity:1/", yours: true, who: "you"},
    {kind: "reacted", at: "2026-09-30T08:00:00Z", reaction: "LIKE", post_url: "https://www.linkedin.com/feed/update/urn:li:activity:2/", yours: false, who: "Mira"},
  ]};
}

// A note after Tin's score makes the score old until the next run reads it.
function noted(person) {
  for (const key of ["icp", "icp_fit"]) {
    const value = person.fields[key];
    if (value?.by === "engagers" && value.value !== null && !value.stale) Object.assign(value, {stale: true, stale_reason: "note"});
  }
}

// `analytics` answers the Analytics page and card: {pages: {today, yesterday, week}, writes: [],
// answer(action, body) → {status, body}} for the card's requests: setup, address, the reporter's
// value, the site builder and starting the analytics change again.
export async function serveApp({decisionCount = 4, people = syntheticPeople(), engagers = false, visitors = false, integrations = [], removed = [], requests = null, requestWrites = [], analytics = null} = {}) {
  const project = {id: "project", name: "Example Co", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const catalog = [{id: "wf-answer", key: "content.generate", title: "Draft planned content", description: "Draft the next planned page for review.", definition: {schedule_modes: ["weekly", "on_demand"]}}];
  // The workflow that finds people is offered only to some projects; People shows with it.
  if (engagers) catalog.push({id: "wf-engagers", key: "social.linkedin_engagers", title: "People who engaged with your LinkedIn posts", description: "Who reacted to your LinkedIn posts.", definition: {schedule_modes: ["daily", "on_demand"]}});
  if (visitors) catalog.push({id: "wf-visitors", key: "product.visitor_journeys", title: "Visitor journeys", description: "Who used your site and product each day.", definition: {schedule_modes: ["daily", "on_demand"]}});
  const projectWorkflows = [{id: "cfg-1", workflow_id: "wf-answer", name: "Draft planned content", status: "active", schedule: {cadence: "weekly", weekdays: ["tuesday"], local_time: "10:00", timezone: "UTC"}, inputs: {}, created_at: "2026-09-20T00:00:00Z"}];
  const runs = Array.from({length: decisionCount}, (_, index) => ({id: `run-${index}`, workflow_id: "wf-answer", workflow_name: "content.generate", status: "needs_input", created_at: `2026-09-2${index}T10:00:00Z`, artifact_path: "reports/answers/page-1.md", canonical_commit_sha: revision}));
  const decisions = runs.map((run, index) => ({
    id: `decision-${index}`, run_id: run.id, project_id: project.id, workflow_key: "content.generate", workflow_title: "Draft planned content",
    kind: "review", title: `Review answer page ${index + 1}`, explanation: "The draft answers which marketing tools work with coding agents. Read it, then approve it or ask for changes.",
    consequence: "Approving keeps the draft in Files.", items: [{file: run.artifact_path, revision, title: `Answer page ${index + 1}`}], created_at: run.created_at,
  }));
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|chat|activity|decisions|people|analytics|files|file|integrations)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8"))
        .replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "")
        .replaceAll("{{BROWSER_LOCK_ENABLED}}", "false").replaceAll("{{MCP_URL}}", "https://app.tin.test/mcp")
        .replaceAll("{{AUTH_RETURN_URL}}", "").replaceAll("{{AUTH_FLOW}}", "product");
      response.setHeader("Content-Type", "text/html");
      return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {
        const body = await fs.readFile(file);
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".wasm") ? "application/wasm" : "application/octet-stream");
        return response.end(body);
      } catch {response.writeHead(404).end(); return;}
    }
    const personPath = url.pathname.match(/^\/api\/projects\/[^/]+\/people\/([^/]+)$/);
    const notePath = url.pathname.match(/^\/api\/projects\/[^/]+\/people\/([^/]+)\/notes(?:\/([^/]+))?$/);
    const read = async () => {
      let text = "";
      for await (const chunk of request) text += chunk;
      return text ? JSON.parse(text) : {};
    };
    const refuse = (status, detail) => {response.statusCode = status; return send({detail});};
    if (notePath) {
      const found = people.find(item => item.id === notePath[1]);
      if (!found) return refuse(404, "This person is no longer in this project.");
      const now = new Date().toISOString();
      if (request.method === "POST") {
        const {body} = await read();
        if (!body?.trim()) return refuse(422, "Write something first.");
        found.notes.unshift({id: `n-${found.notes.length + 1}-${Date.now()}`, body: body.trim(), by: "you", mine: true, at: now, edited: false});
        noted(found);
        response.statusCode = 201;
        return send(detail(found));
      }
      const index = found.notes.findIndex(note => note.id === notePath[2]);
      if (index < 0) return refuse(404, "This note is no longer here.");
      if (!found.notes[index].mine) return refuse(403, "Only the note's author can change it.");
      if (request.method === "PATCH") {
        const {body} = await read();
        if (!body?.trim()) return refuse(422, "Write something first.");
        Object.assign(found.notes[index], {body: body.trim(), edited: true});
        noted(found);
      } else if (request.method === "DELETE") {
        found.notes.splice(index, 1);
      }
      return send(detail(found));
    }
    if (request.method === "PATCH" && personPath) {
      const found = people.find(item => item.id === personPath[1]);
      if (!found) return refuse(404, "This person is no longer in this project.");
      const change = await read();
      const fields = found.fields;
      const now = new Date().toISOString();
      if ("fit" in change) {
        if (change.fit === null) {
          for (const key of ["icp", "icp_fit"]) {
            const previous = fields[key]?.previous;
            if (previous) fields[key] = {...fields[key], value: previous.value, reason: previous.reason, by: "engagers", stale: false, stale_reason: null, previous: null};
          }
        } else {
          const picked = change.fit.icp;
          if (picked !== null && !profiles.includes(picked)) return refuse(422, "Pick one of the profiles in strategy/ICP.md.");
          for (const [key, value] of [["icp", picked ?? "none"], ["icp_fit", picked === null ? 0 : 1]]) {
            const before = fields[key];
            const previous = before?.by === "engagers" ? {value: before.value, reason: before.reason} : before?.previous || null;
            fields[key] = {value, reason: "", by: "you", at: now, stale: false, stale_reason: null, definition: "", previous};
          }
        }
      }
      if ("customer" in change) fields.customer = {value: change.customer ? "yes" : "no", reason: "", by: "you", at: now, stale: false, stale_reason: null, definition: "", previous: null};
      return send(detail(found));
    }
    if (request.method === "DELETE" && personPath) {
      const index = people.findIndex(item => item.id === personPath[1]);
      if (index < 0) {response.statusCode = 404; return send({detail: "This person is no longer in this project."});}
      people.splice(index, 1);
      response.statusCode = 204;
      return response.end();
    }
    const startPath = url.pathname.match(/^\/api\/projects\/[^/]+\/workflows\/([^/]+)\/runs$/);
    if (request.method === "POST" && startPath) {
      const workflow = catalog.find(item => item.id === projectWorkflows.find(saved => saved.id === startPath[1])?.workflow_id);
      return send({id: "run-started", workflow_id: workflow.id, workflow_name: workflow.key, status: "running", created_at: "2026-10-15T10:00:00Z"});
    }
    // Connection requests (#450): the member's sending and the people they choose.
    const requestsPath = url.pathname.match(/^\/api\/projects\/[^/]+\/linkedin-requests(\/.*)?$/);
    if (requestsPath && requests) {
      const rest = requestsPath[1] || "";
      if (request.method === "GET" && !rest) return send(requests);
      const body = request.method === "GET" ? {} : await read();
      requestWrites.push({method: request.method, path: rest, body});
      if (rest === "/sending") {
        if (body.enabled && body.consent_version !== 1) return refuse(422, "Tick the box to agree to sending first.");
        Object.assign(requests.sending, {on: body.enabled, ...(body.limits || {})});
        return send(requests);
      }
      if (rest === "/sending/resume") {
        Object.assign(requests.sending, {paused: false, held: false, paused_reason: null});
        return send(requests);
      }
      // Ticking people in People queues them at once.
      if (rest === "/send") {
        if (!requests.sending.on) return refuse(409, "Turn on sending on the LinkedIn card first.");
        const queued = {queued_at: new Date().toISOString(), sending: false, at: requests.next_open};
        for (const id of body.people) Object.assign(people.find(item => item.id === id) || {}, {requestable: false, request: "queued", queued});
        return send({status: "approved", batch_id: "batch-people", queued: body.people.length, repeated: false, first_send: requests.next_open, last_send: requests.next_open});
      }
      // Taking a queued request back before it goes.
      const cancelled = rest.match(/^\/people\/([^/]+)\/cancel$/);
      if (cancelled) {
        const found = people.find(item => item.id === cancelled[1]);
        if (found?.request !== "queued") return refuse(404, "No request to them is waiting to go out.");
        Object.assign(found, {requestable: true, request: null, queued: null});
        return send({cancelled: 1});
      }
    }
    const analyticsAction = url.pathname.match(/^\/api\/projects\/[^/]+\/integrations\/analytics\.agent_traffic\/(setup|address|reporter-token|builder|install)$/);
    if (analytics && request.method === "POST" && analyticsAction) {
      const body = await read();
      analytics.writes?.push({action: analyticsAction[1], body});
      const answer = analytics.answer(analyticsAction[1], body);
      response.statusCode = answer.status || 200;
      return send(answer.body);
    }
    if (analytics && request.method === "GET" && /^\/api\/projects\/[^/]+\/analytics$/.test(url.pathname)) {
      return send(analytics.pages[url.searchParams.get("view") || "today"]);
    }
    // Disconnecting marks the connection gone, as the API does, and records which.
    const integrationPath = url.pathname.match(/^\/api\/projects\/[^/]+\/integrations\/([^/]+)$/);
    if (request.method === "DELETE" && integrationPath) {
      const found = integrations.find(item => item.key === decodeURIComponent(integrationPath[1]));
      removed.push(decodeURIComponent(integrationPath[1]));
      if (found) Object.assign(found, {connection_id: null, status: "available", configuration: {}});
      response.statusCode = 204;
      return response.end();
    }
    if (request.method !== "GET") {response.statusCode = 204; return response.end();}
    if (/^\/api\/projects\/[^/]+\/integrations$/.test(url.pathname)) return send(integrations);
    if (/^\/api\/projects\/[^/]+\/people$/.test(url.pathname)) return send({people: people.map(listed), truncated: false, fit_shown: 0.6, profiles, icp_path: "strategy/ICP.md"});
    if (personPath) {
      const found = people.find(item => item.id === personPath[1]);
      if (!found) {response.statusCode = 404; return send({detail: "This person is no longer in this project."});}
      return send(detail(found));
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send(catalog);
    if (/\/api\/projects\/[^/]+\/workflows$/.test(url.pathname)) return send(projectWorkflows);
    if (/\/api\/projects\/[^/]+\/runs$/.test(url.pathname)) return send(runs);
    if (url.pathname.endsWith("/decisions")) return send(decisions);
    if (url.pathname.endsWith("/system")) return send({workflow_count: projectWorkflows.length, running_count: 0, waiting_count: decisions.length, runs_this_month: runs.length});
    if (url.pathname.endsWith("/files")) return send({revision, files: projectFiles.map(file => ({path: file}))});
    if (url.pathname.endsWith("/files/document")) {
      return send({filename: url.searchParams.get("path").split("/").at(-1), word_count: 240, reading_minutes: 1, html: documentHtml, related_documents: []});
    }
    if (url.pathname.endsWith("/files/raw")) {
      const file = url.searchParams.get("path");
      const body = file.endsWith(".mmd") ? "flowchart LR\n  A[Plan] --> B[Draft] --> C[Review]\n"
        : file.endsWith(".csv") ? "keyword,volume\ncoding agent marketing,90\n"
          : file.endsWith(".json") ? '{"keywords": ["coding agent marketing"]}\n' : "Plain project notes.\n";
      response.setHeader("Content-Type", file.endsWith(".json") ? "application/json" : "text/plain");
      return response.end(body);
    }
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, people, integrations, removed, requests, requestWrites, base: `http://127.0.0.1:${server.address().port}`};
}

export async function openApp(browser, base, {viewport = {width: 1440, height: 900}, url = "/system", theme = "light"} = {}) {
  const errors = [];
  const context = await browser.newContext({viewport});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(theme => {
    window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", theme);
  }, theme);
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}${url}${url.includes("?") ? "&" : "?"}project=project`);
  return {page, context, errors};
}

export function fileUrl(filePath) {
  return `/file?${new URLSearchParams({path: filePath, revision})}`;
}
