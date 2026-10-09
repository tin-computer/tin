// System › Workflows (Paper WF-A) in the real packaged dashboard; identity and HTTP are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
const schema = {type: "object", properties: {}};
const project = {id: "project", name: "Sheepdogs", workspace_id: "workspace", workspace_name: "Fixture", timezone: "UTC", member_count: 1};
const organic = {system_id: "organic", system_name: "Organic traffic system", system_order: 1};
const workflow = (key, title, description, extra = {}) => ({
  id: key, key, title, description, version_label: "1.0.0", status: "active", executor: "workflow.code",
  allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: ["on_demand"]}, ...organic, ...extra,
});
const workflows = [
  workflow("organic.audit", "Audit organic visibility", "Audit technical SEO and AI visibility (GEO). Read robots.txt, sitemaps and Search Console queries, and check up to 100 public pages by default.", {last_run_id: "audit-run", last_run_at: "2026-09-14T16:00:00Z"}),
  workflow("organic.traffic_snapshot", "Take a weekly traffic snapshot and growth readout", "Each week, read Search Console, and PostHog when it is connected, once into one file. A change is called only when an exact test with Holm's correction says it is real."),
  workflow("organic.content_efficacy", "Decide what each page needs", "Each week, judge every page and ask through website.change before anything changes on the site. Edits nothing itself."),
];

test("the Workflows list shows each description's first sentence and the last run beside the key", async () => {
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
    if (url.pathname === "/api/workflows") return send(workflows);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  const errors = [];
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, timezoneId: "UTC", colorScheme: "dark"});
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic-only"}};
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    await page.locator('[data-workflow-section="registry"]').click();
    const cards = page.locator(".system-template-card");
    await cards.first().waitFor();

    // The first sentence only; a dotted name such as website.change is not a sentence end.
    assert.deepEqual(await page.locator(".system-template-description").allInnerTexts(), [
      "Audit technical SEO and AI visibility (GEO).",
      "Each week, read Search Console, and PostHog when it is connected, once into one file.",
      "Each week, judge every page and ask through website.change before anything changes on the site.",
    ]);
    // The last run sits in the key's line, not under the actions, and still opens the run.
    assert.equal(await cards.nth(0).locator(".system-template-identity code").innerText(), "organic.audit · last run sep 14, 16:00");
    assert.equal(await page.locator(".system-template-side .system-last-run").count(), 0);
    assert.equal(await cards.nth(0).locator("[data-template-last-run]").getAttribute("data-template-last-run"), "audit-run");
    assert.equal(await cards.nth(1).locator(".system-template-identity code").innerText(), "organic.traffic_snapshot");

    // A readable measure, and the actions centred on the card.
    const layout = await cards.nth(0).evaluate((card) => {
      const box = (node) => node.getBoundingClientRect();
      const side = box(card.querySelector(".system-template-side"));
      const own = box(card);
      return {
        measure: Math.round(box(card.querySelector(".system-template-description")).width),
        centre: Math.round((side.top + side.bottom) / 2 - (own.top + own.bottom) / 2),
      };
    });
    assert.ok(layout.measure <= 720, JSON.stringify(layout));
    assert.ok(Math.abs(layout.centre) <= 1, JSON.stringify(layout));

    // Narrow, the card stacks: its text keeps the left edge however short it is.
    await page.setViewportSize({width: 390, height: 900});
    const edges = await page.evaluate(() => [...document.querySelectorAll(".system-template-card")].map((card) =>
      Math.round(card.querySelector(".system-template-identity").getBoundingClientRect().left - card.getBoundingClientRect().left)));
    assert.equal(new Set(edges).size, 1, JSON.stringify(edges));
    await page.setViewportSize({width: 1440, height: 1000});

    // A search that matched only the rest of a description shows all of it.
    await page.locator("#workflow-search").fill("holm");
    await page.locator(".system-template-card").first().waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".system-template-card").length === 1);
    assert.match(await page.locator(".system-template-description").innerText(), /Holm's correction/);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});
