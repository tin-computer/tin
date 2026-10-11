// My system's groups under the calendar (Paper SYS-C) in the real packaged dashboard.
// Identity, clock and HTTP are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
const LA = "America/Los_Angeles";
const NOW = new Date("2026-10-10T23:30:00Z");
const schema = {type: "object", properties: {}};
const project = {id: "project", name: "Tin Computer", workspace_id: "workspace", workspace_name: "Fixture", timezone: LA, member_count: 2};
const workflow = {id: "report", key: "custom.report", title: "Report", description: "Fixture", version_label: "1.0.0", status: "active", executor: "workflow.code", allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: ["on_demand", "weekly"]}};
const traffic = {...workflow, id: "traffic", key: "organic.traffic_system", title: "Run the organic traffic system"};
const weekly = (day, time) => ({cadence: "weekly", weekdays: [day], local_time: time, timezone: LA, start_at: "2026-10-01T00:00:00Z"});
const saved = (id, name, extra = {}) => ({id, project_id: "project", workflow_id: workflow.id, workflow_key: workflow.key, workflow_title: workflow.title, name, version_label: "1.0.0", definition_commit_sha: "a".repeat(40), inputs: {}, input_schema: schema, status: "active", settings_revision: 1, created_at: "2026-10-01T00:00:00Z", schedule: null, run_count: 2, done_count: 2, last_run_status: "succeeded", ...extra});
// Newest first, as the API returns them.
const configured = [
  saved("article", "Weekly article", {schedule: weekly("tuesday", "10:00"), next_run_at: "2026-10-13T17:00:00Z"}),
  saved("refresh", "Weekly page refresh", {schedule: weekly("thursday", "10:00"), next_run_at: "2026-10-15T17:00:00Z", last_run_status: "failed", last_started_at: "2026-10-08T17:00:00Z", last_finished_at: "2026-10-08T17:01:48Z"}),
  saved("decisions", "Weekly page decisions", {schedule: weekly("thursday", "09:00"), next_run_at: "2026-10-15T16:00:00Z"}),
  saved("tracking", "Weekly tracking review", {schedule: weekly("monday", "05:30"), next_run_at: "2026-10-12T12:30:00Z"}),
  saved("signups", "Daily signups", {schedule: {cadence: "daily", local_time: "08:00", timezone: LA, start_at: "2026-10-01T00:00:00Z"}, status: "paused", next_run_at: null}),
  saved("collect", "Collect connections"),
  saved("plan", "Organic traffic"),
];
const runs = [
  {id: "article-run", project_id: "project", workflow_id: workflow.id, workflow_name: workflow.key, project_workflow_id: "article", status: "running", trigger_source: "manual", created_at: "2026-10-10T23:20:00Z", started_at: "2026-10-10T23:20:00Z", progress_summary: "Writing the draft."},
  {id: "api-run", project_id: "project", workflow_id: "traffic", workflow_name: "organic.traffic_system", project_workflow_id: null, status: "running", trigger_source: "api", created_at: "2026-10-10T21:55:00Z", started_at: "2026-10-10T21:55:00Z", progress_summary: "Review the article to continue."},
];

test("My system keeps live runs and failures open and folds the healthy rest", async () => {
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
    if (url.pathname === "/api/workflows") return send([workflow, traffic]);
    if (url.pathname === "/api/projects/project/workflows") return send(configured);
    if (url.pathname === "/api/projects/project/runs") return send(runs);
    if (url.pathname === "/api/projects/project/week") return send({start: url.searchParams.get("start"), timezone: LA, runs: [], occurrences: []});
    if (url.pathname.endsWith("/system")) return send({workflow_count: configured.length, running_count: 2, waiting_count: 0, runs_this_month: 9});
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
    await page.locator(".system-group-fold").first().waitFor();

    const groups = () => page.evaluate(() => [...document.querySelectorAll(".system-workflow-group")].map((group) => {
      const fold = group.querySelector(".system-group-fold");
      if (fold) return {fold: fold.querySelector(".system-card-identity strong").innerText, open: fold.classList.contains("is-open"), cards: group.querySelectorAll(".system-workflow-card:not(.system-group-fold)").length};
      return {header: group.querySelector(":scope > header strong").innerText, titles: [...group.querySelectorAll(".system-card-identity strong")].map((title) => title.innerText)};
    }));
    // A saved workflow's live run joins Running; a failure stays open with its Retry.
    assert.deepEqual(await groups(), [
      {header: "Running", titles: ["Weekly article", "Run the organic traffic system"]},
      {header: "Needs a look", titles: ["Weekly page refresh"]},
      {fold: "Scheduled", open: false, cards: 0},
      {fold: "Available", open: false, cards: 0},
    ]);
    assert.equal(await page.getByRole("button", {name: "Retry", exact: true}).count(), 1);

    // A folded row says what it holds in the card lanes.
    const lanes = (index) => page.locator(".system-group-fold").nth(index).evaluate((fold) =>
      [".system-card-identity code", ".system-card-every", ".system-card-state", ".system-card-last", ".system-action"].map((selector) => fold.querySelector(selector).innerText));
    assert.deepEqual(await lanes(0), ["3 workflows", "weekly, daily", "next mon 05:30", "Next is Weekly tracking review · 1 paused", "Show all"]);
    assert.deepEqual(await lanes(1), ["2 workflows", "manual", "—", "Collect connections, Organic traffic", "Show all"]);
    assert.notEqual(await page.locator(".system-group-fold").first().evaluate((fold) => getComputedStyle(fold).boxShadow), "none");

    // Opened, the group's cards sit in a well under its row, and the browser remembers it.
    const scheduled = page.locator('[data-toggle-system-group="scheduled"]');
    await scheduled.click();
    await page.locator(".system-group-well").waitFor();
    assert.equal(await scheduled.getAttribute("aria-expanded"), "true");
    assert.deepEqual(await page.locator(".system-group-well .system-card-identity strong").allInnerTexts(), ["Scheduled", "Weekly page decisions", "Weekly tracking review", "Daily signups"]);
    assert.equal(await page.locator(".system-group-well .system-action").first().innerText(), "Hide");
    await page.goto(`${base}/?project=project#workflows`);
    await page.locator(".system-group-well").waitFor();
    assert.deepEqual((await groups()).slice(2), [{fold: "Scheduled", open: true, cards: 3}, {fold: "Available", open: false, cards: 0}]);
    await page.locator('[data-toggle-system-group="scheduled"]').click();
    await page.waitForFunction(() => !document.querySelector(".system-group-well"));
    assert.equal(await page.evaluate(() => localStorage.getItem("tin-lite:system-groups")), '{"scheduled":false}');

    // A search lists matches under plain headers; a group of one is a card.
    await page.locator("#workflow-search").fill("collect");
    await page.waitForFunction(() => !document.querySelector(".system-group-fold"));
    assert.deepEqual(await groups(), [{header: "Available", titles: ["Collect connections"]}]);
    await page.locator("#workflow-search").fill("");
    await page.locator(".system-group-fold").first().waitFor();

    // Narrow, the folded row keeps to the screen.
    await page.setViewportSize({width: 390, height: 900});
    const fit = await page.evaluate(() => ({page: document.documentElement.scrollWidth, row: Math.round(document.querySelector(".system-group-fold").getBoundingClientRect().right)}));
    assert.ok(fit.page <= 390 && fit.row <= 390, JSON.stringify(fit));
    assert.equal(await page.locator(".system-group-fold .system-card-identity code").first().isVisible(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});
