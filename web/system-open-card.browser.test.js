// An opened My system card while it runs (Paper OPEN-A1), in the real packaged dashboard.
// Identity, clock and HTTP are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
const LA = "America/Los_Angeles";
const NOW = new Date("2026-10-08T16:30:00Z");
const schema = {
  type: "object",
  properties: {
    execution: {type: "string", title: "Collection mode", enum: ["local_only", "cloud_preferred"], default: "cloud_preferred", description: "Cloud preferred uses cloud collection with a local backup when possible."},
    keywords: {type: "string", title: "Keywords (optional)", description: "Narrow the search with a term such as founder."},
  },
};
const project = {id: "project", name: "ClawMessenger", workspace_id: "workspace", workspace_name: "Fixture", timezone: "UTC", member_count: 1};
const workflow = {id: "collect", key: "connections.collect", title: "Collect connections", description: "Fixture", version_label: "1.0.0", status: "active", executor: "workflow.code", allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: ["on_demand"]}};
const configured = {id: "saved", project_id: "project", workflow_id: workflow.id, workflow_key: workflow.key, workflow_title: workflow.title, name: "Collect connections", version_label: "1.0.0", definition_commit_sha: "a".repeat(40), inputs: {execution: "cloud_preferred", keywords: "founder"}, input_schema: schema, status: "active", settings_revision: 1, created_at: "2026-10-01T00:00:00Z", schedule: null, next_run_at: null, run_count: 3, done_count: 3, last_artifact_path: "connections/run/manifest.json", last_run_status: "succeeded"};
const run = {id: "run", project_id: "project", workflow_id: workflow.id, workflow_name: workflow.key, project_workflow_id: "saved", status: "running", trigger_source: "manual", created_at: "2026-10-08T16:16:00Z", started_at: "2026-10-08T16:16:00Z", progress_summary: "Collecting connections. 1 page saved."};

test("an opened running card keeps Open in its row and one rhythm in its settings", async () => {
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = (data) => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(data));};
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html");
      return response.end((await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "").replaceAll("{{BILLING_ENABLED}}", "false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {
        const data = await fs.readFile(file);
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(data);
      } catch {
        response.writeHead(404).end();
        return;
      }
    }
    if (request.method !== "GET") {
      response.statusCode = 400;
      return send({detail: "Unexpected write"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
    if (url.pathname === "/api/projects/project/runs") return send([run]);
    if (url.pathname === "/api/projects/project/week") return send({start: url.searchParams.get("start"), timezone: LA, runs: [], occurrences: []});
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 1, waiting_count: 0, runs_this_month: 4});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  const errors = [];
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, timezoneId: LA, colorScheme: "dark"});
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic-only"}};
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => errors.push(error.message));
    await page.clock.setFixedTime(NOW);
    await page.goto(`${base}/?project=project#workflows`);

    // Closed: Open is a row action, the line below is the sentence alone, and it does not
    // repeat "manual", which the schedule lane already says.
    const card = page.locator(".system-workflow-card.is-running").first();
    await card.waitFor();
    assert.equal(await card.locator(".system-card-row .system-card-actions .system-action").innerText(), "Open");
    assert.equal(await card.locator(".system-running-detail button").count(), 0);
    assert.equal(await card.locator(".system-running-detail").innerText(), "Collecting connections. 1 page saved. Started 09:16.");

    // Opened: the same header, settings with no eyebrows on one rhythm at the card's edge.
    await card.locator(".system-card-identity").click();
    const form = page.locator(".system-config-form.is-running");
    await form.waitFor();
    assert.equal(await form.locator(".system-card-row .system-action").innerText(), "Open");
    assert.equal(await form.locator(".system-config-kicker").count(), 0);
    const geometry = await form.evaluate((element) => {
      const box = (node) => node.getBoundingClientRect();
      const card = box(element);
      const settings = [...element.querySelectorAll(".system-config-body > section:first-child > .system-setting")].map(box);
      const firstLabel = element.querySelector(".system-config-body > section:first-child .system-setting > label");
      const runsLabel = element.querySelector(".system-config-when .schedule-mode .workflow-row-label");
      const mode = element.querySelector(".system-config-when .schedule-mode");
      const fact = element.querySelector(".system-config-when > p:not([data-workflow-cost])");
      const help = element.querySelector(".system-setting .workflow-field-message");
      return {
        bodyEdge: Math.round(box(element.querySelector(".system-config-body > section:first-child .system-setting")).left - card.left),
        footerEdge: Math.round(box(element.querySelector(".system-config-footer > *")).left - card.left),
        sentenceEdge: Math.round(box(element.querySelector(".system-running-detail > span")).left - box(element.querySelector(".system-card-identity strong")).left),
        labelHeight: Math.round(box(firstLabel).height),
        labelTops: Math.round(box(firstLabel).top - box(runsLabel).top),
        gaps: settings.slice(1).map((setting, index) => Math.round(setting.top - settings[index].bottom)),
        factGap: Math.round(box(fact).top - box(mode).bottom),
        helpColor: getComputedStyle(help).color,
        secondary: getComputedStyle(element.querySelector(".system-running-detail")).color,
      };
    });
    assert.equal(geometry.bodyEdge, 17, JSON.stringify(geometry));
    assert.equal(geometry.footerEdge, 17, JSON.stringify(geometry));
    assert.equal(geometry.sentenceEdge, 0, JSON.stringify(geometry));
    assert.equal(geometry.labelHeight, 16, JSON.stringify(geometry));
    assert.equal(geometry.labelTops, 0, JSON.stringify(geometry));
    assert.ok(geometry.gaps.length >= 2 && geometry.gaps.every((gap) => gap === 20), JSON.stringify(geometry));
    assert.equal(geometry.factGap, 6, JSON.stringify(geometry));
    assert.equal(geometry.helpColor, geometry.secondary, JSON.stringify(geometry));

    // Open still shows the run's details from its new place, as before, and then reads Close.
    await form.locator(".system-card-row .system-action").click();
    await page.locator(".system-workflow-card.is-running .system-card-row .system-action", {hasText: "Close"}).waitFor();
    assert.equal(await page.locator(".system-workflow-card.is-running .system-run-detail").count(), 1);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});
