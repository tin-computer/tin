// The System calendar in the real packaged dashboard; identity, clock and HTTP are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
const LA = "America/Los_Angeles";
// Wednesday 7 October 2026, 12:18 in Los Angeles.
const NOW = new Date("2026-10-07T19:18:00Z");
const schema = {type: "object", properties: {}};
const project = {id: "project", name: "Sheepdogs", workspace_id: "workspace", workspace_name: "Fixture", timezone: "UTC", member_count: 1};
const article = {id: "article", key: "content.generate", title: "Draft planned content", description: "Fixture", version_label: "1.10.0", status: "active", executor: "codex.procedure", allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: ["on_demand", "weekly"]}};
const saved = (id, name, weekday) => ({id, project_id: "project", workflow_id: article.id, workflow_key: article.key, workflow_title: article.title, name, version_label: "1.10.0", definition_commit_sha: "a".repeat(40), inputs: {}, input_schema: schema, status: "active", settings_revision: 1, created_at: "2026-09-16T00:00:00Z", schedule: {cadence: "weekly", weekdays: [weekday], local_time: "10:00", timezone: LA, start_at: "2026-09-16T00:00:00Z", end_at: null}, next_run_at: null, run_count: 0, done_count: 0});
const configured = [saved("weekly", "Weekly article", "tuesday"), saved("refresh", "Weekly page refresh", "thursday")];

let sequence = 0;
const run = (at, status, extra = {}) => ({
  id: `run-${++sequence}`, project_workflow_id: "weekly", workflow_key: article.key, workflow_title: article.title,
  status, trigger_source: "manual", at, finished_at: null, title: null, summary: null, progress_current: null, progress_total: null, ...extra,
});
// Monday is busy: one review, four results and two stopped drafts. Monday 23:05 in Los
// Angeles is already Tuesday in UTC, and still belongs to Monday.
const week = {
  start: "2026-10-05",
  timezone: LA,
  runs: [
    run("2026-10-05T16:00:00Z", "succeeded", {title: "Plan checked", summary: "Next up: Herding at night"}),
    run("2026-10-05T17:30:00Z", "stopped", {title: "Draft one"}),
    run("2026-10-05T18:00:00Z", "succeeded", {title: "Article PR opened"}),
    run("2026-10-05T19:00:00Z", "stopped", {title: "Draft two"}),
    run("2026-10-05T20:00:00Z", "succeeded", {title: "Brand guide captured"}),
    run("2026-10-05T21:00:00Z", "succeeded", {title: "How I filmed the trailers"}),
    run("2026-10-06T06:05:00Z", "needs_input", {title: "How to herd sheep in Sheepdogs", summary: "Public article"}),
    run("2026-10-06T17:00:00Z", "failed", {summary: "The site did not answer"}),
    run("2026-10-07T15:30:00Z", "succeeded", {title: "Keyword plan", summary: "38 groups in Files"}),
    run("2026-10-07T18:50:00Z", "running", {project_workflow_id: "refresh", summary: "Drafting, step 2 of 4"}),
  ],
  occurrences: [
    {project_workflow_id: "refresh", name: "Weekly page refresh", workflow_key: article.key, at: "2026-10-08T17:00:00Z", state: "planned", lands: "Files"},
    {project_workflow_id: "weekly", name: "Weekly article", workflow_key: article.key, at: "2026-10-08T18:00:00Z", state: "held", lands: "Decisions"},
    // Saturday has more than a day shows, so its day opens from the right-hand columns.
    ...["16", "17", "18", "19"].map((hour) => ({project_workflow_id: "refresh", name: `Daily check ${hour}`, workflow_key: article.key, at: `2026-10-10T${hour}:00:00Z`, state: "planned", lands: "Files"})),
  ],
};

async function serve(requests) {
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
    if (url.pathname === "/api/workflows") return send([article]);
    if (url.pathname === "/api/projects/project/workflows") return send(configured);
    if (url.pathname === "/api/projects/project/week") {
      requests.push(Object.fromEntries(url.searchParams));
      const start = url.searchParams.get("start");
      return send(start === week.start ? week : {start, timezone: LA, runs: [], occurrences: []});
    }
    if (url.pathname.endsWith("/system")) return send({workflow_count: 2, running_count: 1, waiting_count: 1, runs_this_month: 9});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {server, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base, viewport, colorScheme = "dark") {
  const context = await browser.newContext({viewport, timezoneId: LA, colorScheme});
  await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(() => {
    window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic-only"}};
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  await page.clock.setFixedTime(NOW);
  await page.goto(`${base}/?project=project#workflows`);
  await page.locator("#system-week:not(.is-loading)").waitFor();
  return page;
}

const day = (page, iso) => page.locator(`.system-week-day[data-week-day="${iso}"]`);

test("the System calendar shows the week as cards, history as lines, and a busy day opened", async () => {
  const requests = [];
  const errors = [];
  const {server, base} = await serve(requests);
  const browser = await chromium.launch({headless: true});
  try {
    const page = await open(browser, base, {width: 1440, height: 1000});
    page.on("pageerror", (error) => errors.push(error.message));

    // The week starts on the viewer's Monday, asked for in the viewer's zone. No heading, no
    // pace line: two arrows sit where the tabs end.
    assert.deepEqual(requests[0], {start: "2026-10-05", timezone: LA});
    assert.equal(await page.locator(".system-pace").count(), 0);
    assert.equal(await page.locator("#system-week h2, #system-week header").count(), 0);
    assert.equal(await page.getByRole("button", {name: "Previous week"}).isVisible(), true);
    assert.equal(await page.getByRole("button", {name: "Today"}).count(), 0);
    assert.deepEqual(await page.locator(".system-week-date").allInnerTexts(), ["mon 5", "tue 6", "wed 7", "thu 8", "fri 9", "sat 10", "sun 11"]);
    assert.equal(await page.locator(".system-week-day.is-today").getAttribute("data-week-day"), "2026-10-07");

    // Monday: the review first as a card, then finished history as lines; stopped runs and
    // the rest wait behind "+N more". The 23:05 review stays on Monday in Los Angeles.
    const monday = day(page, "2026-10-05");
    assert.equal(await monday.locator(".system-week-card.is-for-you strong").innerText(), "How to herd sheep in Sheepdogs");
    assert.match(await monday.locator(".system-week-card.is-for-you small").innerText(), /23:05\s+for you/);
    assert.equal(await monday.locator(".system-week-card.is-for-you > span").innerText(), "Public article");
    assert.deepEqual(await monday.locator(".system-week-chip").allInnerTexts(), ["Plan checked", "Article PR opened"]);
    assert.equal(await monday.locator(".system-week-more").innerText(), "+4 more");
    // Tuesday's failure keeps a card although the day has passed; today's results are cards.
    assert.equal(await day(page, "2026-10-06").locator(".system-week-card.is-failed").count(), 1);
    const today = day(page, "2026-10-07");
    assert.equal(await today.locator(".system-week-card.is-running strong").innerText(), "Weekly page refresh");
    assert.equal(await today.locator(".system-week-card.is-done strong").innerText(), "Keyword plan");
    assert.equal(await today.locator(".system-week-chip").count(), 0);
    // Still to come, at the schedule's own time in the viewer's zone, and where it lands.
    const thursday = day(page, "2026-10-08");
    assert.deepEqual(await thursday.locator(".system-week-card strong").allInnerTexts(), ["Weekly page refresh", "Weekly article"]);
    assert.match(await thursday.locator(".system-week-card.is-planned").innerText(), /Lands in Files\s+10:00\s+planned/);
    assert.match(await thursday.locator(".system-week-card.is-held").innerText(), /Waits for your review first/);

    // Seven equal columns inside the page, nothing spilling sideways.
    const layout = await page.evaluate(() => {
      const main = document.querySelector("#main").getBoundingClientRect();
      const widths = [...document.querySelectorAll(".system-week-day")].map((element) => Math.round(element.getBoundingClientRect().width));
      return {widths, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, right: document.querySelector("#system-week").getBoundingClientRect().right, mainRight: main.right};
    });
    assert.equal(new Set(layout.widths).size, 1, JSON.stringify(layout));
    assert.ok(layout.overflow <= 0, JSON.stringify(layout));
    assert.ok(layout.right <= layout.mainRight);

    // "+4 more" lifts the whole day over the calendar without moving the page below it.
    const below = await page.locator(".system-workflow-group").first().boundingBox();
    await monday.locator(".system-week-more").click();
    const sheet = monday.locator(".system-week-sheet");
    await sheet.waitFor();
    assert.equal(await monday.locator(".system-week-more").getAttribute("aria-expanded"), "true");
    assert.equal(await sheet.locator("header > span").innerText(), "mon 5 · 7 runs");
    assert.equal(await sheet.locator(".system-week-card:not(.is-folded)").count(), 5);
    assert.match(await sheet.locator(".system-week-card.is-folded").innerText(), /2 stopped\s+Draft one, Draft two/);
    assert.deepEqual(await page.locator(".system-workflow-group").first().boundingBox(), below);
    const placed = await page.evaluate(() => {
      const box = document.querySelector(".system-week-sheet").getBoundingClientRect();
      const column = document.querySelector('.system-week-day[data-week-day="2026-10-05"]').getBoundingClientRect();
      const card = document.querySelector(".system-week-sheet .system-week-card").getBoundingClientRect();
      return {sheetRight: box.right, mainRight: document.querySelector("#main").getBoundingClientRect().right, cardLeft: card.left, columnLeft: column.left};
    });
    assert.ok(placed.sheetRight <= placed.mainRight, JSON.stringify(placed));
    assert.ok(Math.abs(placed.cardLeft - placed.columnLeft) <= 1, JSON.stringify(placed));

    // Escape closes it and returns focus to "+4 more"; so does a click elsewhere.
    await page.keyboard.press("Escape");
    assert.equal(await sheet.count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.weekMore), "2026-10-05");
    await monday.locator(".system-week-more").click();
    await sheet.waitFor();
    await page.locator(".system-header h1").click();
    assert.equal(await sheet.count(), 0);

    // Saturday's day opens leftward, its right edge on the column's, inside the page.
    const saturday = day(page, "2026-10-10");
    assert.equal(await saturday.locator(".system-week-more").innerText(), "+1 more");
    await saturday.locator(".system-week-more").click();
    await saturday.locator(".system-week-sheet").waitFor();
    assert.equal(await saturday.locator(".system-week-sheet header > span").innerText(), "sat 10 · 4 scheduled");
    const leftward = await page.evaluate(() => {
      const box = document.querySelector(".system-week-sheet").getBoundingClientRect();
      const column = document.querySelector('.system-week-day[data-week-day="2026-10-10"]').getBoundingClientRect();
      return {sheetRight: box.right, columnRight: column.right, weekRight: document.querySelector("#system-week").getBoundingClientRect().right};
    });
    assert.ok(Math.abs(leftward.sheetRight - leftward.columnRight - 13) <= 1, JSON.stringify(leftward));
    assert.ok(leftward.sheetRight <= leftward.weekRight + 13, JSON.stringify(leftward));
    await saturday.locator("[data-week-close]").click();
    assert.equal(await saturday.locator(".system-week-sheet").count(), 0);

    // The arrows walk weeks and "Today" comes back. A week already read is not asked for
    // again; polling refreshes the week on screen.
    await page.getByRole("button", {name: "Next week"}).click();
    await page.locator('.system-week-day[data-week-day="2026-10-12"]').waitFor();
    await page.getByRole("button", {name: "Today"}).click();
    await page.getByRole("button", {name: "Next week"}).click();
    await page.locator("#system-week:not(.is-loading)").waitFor();
    assert.equal(requests.filter((request) => request.start === "2026-10-12").length, 1);
    await page.getByRole("button", {name: "Today"}).click();
    await page.getByRole("button", {name: "Previous week"}).click();
    await page.locator('.system-week-day[data-week-day="2026-09-28"]').waitFor();
    await page.locator("#system-week:not(.is-loading)").waitFor();
    assert.deepEqual(requests.at(-1), {start: "2026-09-28", timezone: LA});
    assert.equal(await page.locator(".system-week-day.is-today").count(), 0);
    await page.getByRole("button", {name: "Today"}).click();
    assert.equal(await page.locator(".system-week-day.is-today").getAttribute("data-week-day"), "2026-10-07");
    assert.equal(await page.getByRole("button", {name: "Today"}).count(), 0);

    // A slot still to come opens its saved workflow's settings.
    await thursday.locator(".system-week-card.is-planned").click();
    await page.locator('.system-config-form[data-project-workflow-id="refresh"], .workflow-config-ledger[data-project-workflow-id="refresh"]').first().waitFor();
    await page.context().close();

    // Narrow: the days stack, empty days fold away, and nothing scrolls sideways.
    const narrow = await open(browser, base, {width: 390, height: 900}, "light");
    narrow.on("pageerror", (error) => errors.push(error.message));
    const stacked = await narrow.evaluate(() => ({
      shown: [...document.querySelectorAll(".system-week-day")].filter((element) => element.offsetParent).map((element) => element.dataset.weekDay),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    assert.deepEqual(stacked.shown, ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-10"]);
    assert.ok(stacked.overflow <= 0, JSON.stringify(stacked));
    await day(narrow, "2026-10-05").locator(".system-week-more").click();
    await day(narrow, "2026-10-05").locator(".system-week-sheet").waitFor();
    assert.equal(await day(narrow, "2026-10-05").locator(".system-week-more").isVisible(), false);
    assert.ok(await narrow.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth) <= 0);
    await narrow.context().close();

    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});
