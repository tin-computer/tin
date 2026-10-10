// Type in workflow forms (Paper FT-A) in the real packaged dashboard; identity and HTTP are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
const project = {id: "project", name: "Fixture project", workspace_id: "workspace", workspace_name: "Fixture", timezone: "UTC", member_count: 1};
const schema = {type: "object", properties: {
  audience: {type: "string", title: "Audience"},
  site_url: {type: "string", format: "uri", title: "Site URL"},
  tone: {type: "string", enum: ["plain", "warm"], title: "Tone"},
  mode: {type: "string", enum: ["draft", "publish"], title: "Mode", "x-tin-ui": {control: "segmented"}},
  alternatives: {type: "integer", minimum: 1, maximum: 9, title: "Alternatives", "x-tin-ui": {control: "counter"}},
  topics: {type: "array", title: "Topics", items: {type: "string"}},
  platforms: {type: "array", title: "Where to look", items: {type: "string", enum: ["reddit", "hacker_news"]}},
}};
const workflow = {id: "code", key: "custom.posts", title: "Draft posts", description: "Fixture posts", version_label: "1.0", status: "active", executor: "workflow.code", allowed_actions: ["start", "save"], definition: {executor: "workflow.code", input_schema: schema, schedule_modes: ["on_demand", "weekly"]}};
const configured = {id: "saved", project_id: "project", workflow_id: "code", workflow_key: workflow.key, workflow_title: workflow.title, name: "Weekly posts", definition_commit_sha: "a".repeat(40), inputs: {audience: "Founders", site_url: "https://example.com", tone: "warm", mode: "draft", alternatives: 6, topics: ["pricing", "onboarding"], platforms: ["reddit", "hacker_news"]}, input_schema: schema, status: "active", settings_revision: 1, created_at: "2026-10-01T00:00:00Z", schedule: {cadence: "weekly", weekdays: ["tuesday"], local_time: "09:00", timezone: "America/Los_Angeles", start_at: "2026-10-01T00:00:00Z"}, next_run_at: "2026-10-13T16:00:00Z", run_count: 0, done_count: 0};

test("workflow forms set words in sans and keep mono for keys, paths, URLs, times and timezones", async () => {
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
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".woff2") ? "font/woff2" : "application/octet-stream");
        return response.end(data);
      } catch {
        response.writeHead(404).end();
        return;
      }
    }
    if (request.method === "POST" && url.pathname.endsWith("/workflow-setup")) return send({schedule_modes: ["on_demand", "weekly"], input_schema: schema, can_run: true, can_schedule: true, connections: [], issues: [], schedule_issues: [], estimate: {estimated_usd: "0.00", basis: "included_bounded_compute", external_provider_cost: "not_applicable"}});
    if (request.method !== "GET") {
      response.statusCode = 400;
      return send({detail: "Unexpected write"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  const errors = [];
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, colorScheme: "dark"});
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic-only"}};
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    await page.getByRole("button", {name: "Open Weekly posts settings", exact: true}).click();
    const form = page.locator(".system-config-form");
    await form.locator('[name="input:audience"]').waitFor();

    const type = (locator) => locator.evaluate((node) => {
      const style = getComputedStyle(node);
      return `${/mono/i.test(style.fontFamily) ? "mono" : "sans"} ${style.fontSize}`;
    });
    // Words: the name, free text, choices and counts.
    assert.equal(await type(form.getByLabel("Name", {exact: true})), "sans 13px");
    assert.equal(await type(form.locator('[name="input:audience"]')), "sans 13px");
    assert.equal(await type(form.locator(".tin-select-trigger").first()), "sans 13px");
    assert.equal(await type(form.locator(".tin-select-option").first()), "sans 13px");
    assert.equal(await type(form.locator(".tin-segment").first()), "sans 12.5px");
    assert.equal(await type(form.locator(".tin-counter input")), "sans 13px");
    assert.equal(await type(form.locator('[name="input:topics"]')), "sans 13px");
    // Tabular figures in the sans draw a footed 1 that reads as mono.
    assert.equal(await form.locator(".tin-counter input").evaluate((node) => getComputedStyle(node).fontVariantNumeric), "normal");
    // Literals someone must get exactly right stay mono, a step smaller.
    assert.equal(await type(form.locator('[name="input:site_url"]')), "mono 12px");
    assert.equal(await type(form.locator('[name="input:platforms"]')), "mono 12px");
    assert.equal(await type(form.locator('[name="schedule_time"]')), "mono 12px");
    assert.equal(await type(form.locator('[name="schedule_timezone"]')), "mono 12px");

    // A counter and a choice keep their own width, and the count has no browser border.
    const sizes = await form.evaluate((node) => ({
      counter: Math.round(node.querySelector(".tin-counter").getBoundingClientRect().width),
      segmented: Math.round(node.querySelector(".system-setting .tin-segmented").getBoundingClientRect().width),
      segments: Math.round([...node.querySelectorAll(".system-setting .tin-segment")].reduce((sum, segment) => sum + segment.getBoundingClientRect().width, 0)),
      countBorder: getComputedStyle(node.querySelector(".tin-counter input")).borderTopWidth,
    }));
    assert.equal(sizes.counter, 116, JSON.stringify(sizes));
    assert.ok(sizes.segmented - sizes.segments <= 2, JSON.stringify(sizes));
    assert.equal(sizes.countBorder, "0px");

    // Placeholders draw in the muted ink at full strength, not the browser's own grey.
    const audience = form.locator('[name="input:audience"]');
    await audience.evaluate((node) => {node.value = ""; node.placeholder = "Who reads it";});
    await page.mouse.move(0, 0);
    const brightest = await page.evaluate(async (png) => {
      const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${png}`)).blob());
      const canvas = new OffscreenCanvas(image.width, image.height);
      const context = canvas.getContext("2d");
      context.drawImage(image, 0, 0);
      const data = context.getImageData(3, 3, image.width - 6, image.height - 6).data;
      let best = [0, 0, 0];
      for (let index = 0; index < data.length; index += 4) {
        if (data[index] + data[index + 1] + data[index + 2] > best[0] + best[1] + best[2]) best = [data[index], data[index + 1], data[index + 2]];
      }
      return best;
    }, (await audience.screenshot()).toString("base64"));
    const muted = [133, 124, 111];
    assert.ok(brightest.every((channel, index) => Math.abs(channel - muted[index]) <= 8), JSON.stringify(brightest));
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});
