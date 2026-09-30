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

export async function serveApp({decisionCount = 4} = {}) {
  const project = {id: "project", name: "Example Co", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const catalog = [{id: "wf-answer", key: "content.answer_page", title: "Draft an answer page", description: "Draft one answer page for review.", definition: {schedule_modes: ["weekly", "on_demand"]}}];
  const projectWorkflows = [{id: "cfg-1", workflow_id: "wf-answer", name: "Draft an answer page", status: "active", schedule: {cadence: "weekly", weekdays: ["tuesday"], local_time: "10:00", timezone: "UTC"}, inputs: {}, created_at: "2026-09-20T00:00:00Z"}];
  const runs = Array.from({length: decisionCount}, (_, index) => ({id: `run-${index}`, workflow_id: "wf-answer", workflow_name: "content.answer_page", status: "needs_input", created_at: `2026-09-2${index}T10:00:00Z`, artifact_path: "reports/answers/page-1.md", canonical_commit_sha: revision}));
  const decisions = runs.map((run, index) => ({
    id: `decision-${index}`, run_id: run.id, project_id: project.id, workflow_key: "content.answer_page", workflow_title: "Draft an answer page",
    kind: "review", title: `Review answer page ${index + 1}`, explanation: "The draft answers which marketing tools work with coding agents. Read it, then approve it or ask for changes.",
    consequence: "Approving keeps the draft in Files.", items: [{file: run.artifact_path, revision, title: `Answer page ${index + 1}`}], created_at: run.created_at,
  }));
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|chat|activity|decisions|files|file|integrations)?$/.test(url.pathname)) {
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
    if (request.method !== "GET") {response.statusCode = 204; return response.end();}
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
  return {server, base: `http://127.0.0.1:${server.address().port}`};
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
