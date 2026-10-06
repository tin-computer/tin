// Real packaged dashboard in Chromium; identity and HTTP responses are fixtures.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const flow = {
  direction: "TD",
  nodes: [
    {id: "select", kind: "step", label: "Pick the next plan item", fact: "pins the brief, your style and delivery"},
    {id: "write", kind: "step", label: "Check coverage, then draft", fact: "Codex sandbox, reads up to 12 pages"},
    {id: "files", kind: "store", label: "Draft in project Files", fact: "or a note on why no draft is needed"},
    {id: "review", kind: "gate", label: "Approve the draft", fact: "pick PR, commit to main, or keep in Tin"},
    {id: "pull_request", kind: "surface", label: "Pull request", fact: "GitHub, left unmerged for you"},
    {id: "commit", kind: "surface", label: "Commit to main", fact: "GitHub, on the default branch"},
    {id: "receipt", kind: "receipt", label: "Delivery receipt", fact: "PR number or commit on the run"},
  ],
  edges: [
    {from: "select", to: "write", kind: "call"},
    {from: "write", to: "files", kind: "call"},
    {from: "files", to: "review", kind: "call"},
    {from: "review", to: "write", kind: "signal", label: "request changes"},
    {from: "review", to: "pull_request", kind: "signal"},
    {from: "review", to: "commit", kind: "signal"},
    {from: "pull_request", to: "receipt", kind: "call"},
    {from: "commit", to: "receipt", kind: "call"},
  ],
};

test("a saved workflow's diagram opens beside the System page and reads top to bottom", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id: "project", name: "Fixture project", workspace_id: "workspace", workspace_name: "Fixture", timezone: "UTC", member_count: 1};
  const schema = {type: "object", properties: {}};
  const modes = ["on_demand", "weekly"];
  const drawn = {id: "article", key: "content.generate", title: "Draft planned content", description: "Fixture", version_label: "1.10.0", status: "active", executor: "codex.procedure", allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: modes, presentation: {flow}}};
  const plain = {id: "report", key: "custom.report", title: "Order report", description: "Fixture", version_label: "1.0", status: "active", executor: "workflow.code", allowed_actions: ["start", "save"], definition: {input_schema: schema, schedule_modes: modes}};
  const saved = (id, workflow, name) => ({id, project_id: "project", workflow_id: workflow.id, workflow_key: workflow.key, workflow_title: workflow.title, name, version_label: workflow.version_label, definition_commit_sha: "a".repeat(40), inputs: {}, input_schema: schema, status: "active", settings_revision: 1, created_at: "2026-09-16T00:00:00Z", schedule: {cadence: "weekly", weekdays: ["tuesday"], local_time: "10:00", timezone: "UTC", start_at: "2026-09-16T00:00:00Z", end_at: null}, next_run_at: "2026-10-06T10:00:00Z", run_count: 0, done_count: 0});
  const configured = [saved("weekly", drawn, "Weekly article"), saved("orders", plain, "Order report")];
  const errors = [];
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
    if (url.pathname === "/api/workflows") return send([drawn, plain]);
    if (url.pathname === "/api/projects/project/workflows") return send(configured);
    // The schedule is pinned to an older version than today's drawing.
    if (url.pathname === "/api/projects/project/workflows/weekly/diagram") {
      return send({flow, version: "1.15.0", pinned_version: "1.10.0", exact: false});
    }
    if (url.pathname.endsWith("/system")) return send({workflow_count: 2, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic-only"}};
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);

    const button = page.getByRole("button", {name: "Workflow diagram for Weekly article", exact: true});
    await button.waitFor();
    // Only a workflow whose definition carries a flow gets the button.
    assert.equal(await page.getByRole("button", {name: "Workflow diagram for Order report", exact: true}).count(), 0);
    assert.equal(await button.getAttribute("aria-pressed"), "false");

    await button.click();
    const panel = page.locator("#workflow-diagram-panel");
    await panel.waitFor();
    await panel.evaluate((element) => Promise.all(element.getAnimations().map((animation) => animation.finished)));
    assert.equal(await panel.getByRole("heading", {name: "Weekly article", exact: true}).isVisible(), true);
    // No eyebrow: the title leads, and one line says which version drew the schedule.
    assert.equal(await panel.getByText("How it runs", {exact: false}).count(), 0);
    await panel.locator(".diagram-panel-canvas:not(.is-loading)").waitFor();
    // Connectors are drawn on the next frame, after the cards are laid out.
    await panel.locator(".spine-heads path").first().waitFor({state: "attached"});
    assert.equal(await panel.locator("header code").innerText(), "content.generate · runs v1.10.0 · drawn from v1.15.0");
    assert.equal(await panel.locator(".spine-trigger").innerText(), "every tue · 10:00");
    assert.equal(await panel.locator(".spine-node").count(), 7);
    assert.equal(await panel.locator("footer code").innerText(), "6 steps · 1 approval");
    assert.equal(await button.getAttribute("aria-pressed"), "true");
    // The diagram is not the settings: the row stays a row.
    assert.equal(await page.locator(".workflow-config-ledger, .system-config-form").count(), 0);
    assert.equal(await page.locator(".system-workflow-card.is-diagram-open").count(), 1);

    // The final vocabulary: open chevrons on the line's stroke, a dot instead of an
    // eyebrow on the gate, and a file mark instead of a cylinder on the store.
    const marks = await page.evaluate(() => ({
      heads: [...document.querySelectorAll(".spine-heads path")].map((head) => getComputedStyle(head).fill),
      eyebrows: document.querySelectorAll(".spine-eyebrow").length,
      gateDot: Boolean(document.querySelector(".spine-node.is-gate .spine-dot")),
      storeMark: Boolean(document.querySelector(".spine-node.is-store .spine-node-icon")),
      factFont: getComputedStyle(document.querySelector(".spine-node code")).fontFamily,
    }));
    assert.ok(marks.heads.length && marks.heads.every((fill) => fill === "none"));
    assert.equal(marks.eyebrows, 0);
    assert.equal(marks.gateDot, true);
    assert.equal(marks.storeMark, true);
    assert.doesNotMatch(marks.factFont, /mono/i);

    // Layout B: the page narrows beside the panel instead of running under it.
    const layout = await page.evaluate(() => ({
      mainRight: document.querySelector("#main").getBoundingClientRect().right,
      panelLeft: document.querySelector("#workflow-diagram-panel").getBoundingClientRect().left,
      lastColumn: getComputedStyle(document.querySelector(".system-card-last")).display,
      shortPace: getComputedStyle(document.querySelector(".system-pace-short")).display,
    }));
    assert.ok(layout.mainRight <= layout.panelLeft + 1, `content runs under the panel: ${JSON.stringify(layout)}`);
    assert.equal(layout.lastColumn, "none");
    assert.notEqual(layout.shortPace, "none");

    // One spine: single rows sit on the centre line, a pair sits either side
    // of it, and every connector has been drawn.
    const geometry = await page.evaluate(() => {
      const diagram = document.querySelector(".spine-diagram").getBoundingClientRect();
      const centre = diagram.left + diagram.width / 2;
      const rows = [...document.querySelectorAll(".spine-row:not(.is-trigger)")].map((row) =>
        [...row.querySelectorAll(".spine-node")].map((node) => {
          const rect = node.getBoundingClientRect();
          return rect.left + rect.width / 2 - centre;
        }));
      return {rows, paths: document.querySelectorAll(".spine-lines path").length, panelWidth: document.querySelector("#workflow-diagram-panel").getBoundingClientRect().width};
    });
    assert.equal(geometry.panelWidth, 420);
    assert.deepEqual(geometry.rows.map((row) => row.length), [1, 1, 1, 1, 2, 1]);
    for (const row of geometry.rows) {
      if (row.length === 1) assert.ok(Math.abs(row[0]) < 1, `single node off the spine by ${row[0]}`);
      else assert.ok(Math.abs(row[0] + row[1]) < 1 && row[0] < 0 && row[1] > 0, `pair not balanced: ${row}`);
    }
    // eight edges plus the schedule's signal into the first step
    assert.equal(geometry.paths, 9);

    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_DIAGRAM_PANEL_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_DIAGRAM_PANEL_SCREENSHOTS}/panel-${width}.png`});
    }
    assert.equal(await panel.evaluate((element) => element.getBoundingClientRect().width), 390);
    await page.setViewportSize({width: 1440, height: 1000});

    await page.keyboard.press("Escape");
    await panel.waitFor({state: "detached"});
    assert.equal(await button.getAttribute("aria-pressed"), "false");
    assert.equal(await button.evaluate((element) => element === document.activeElement), true);

    // Opening the settings is its own click, and the diagram can open beside them.
    await page.getByRole("button", {name: "Open Weekly article settings", exact: true}).click();
    const settings = page.locator(".system-config-form");
    await settings.waitFor();
    await settings.getByRole("button", {name: "Workflow diagram for Weekly article", exact: true}).click();
    await panel.waitFor();
    // the settings stay open beside the diagram
    assert.equal(await settings.isVisible(), true);
    await panel.getByRole("button", {name: "Close the workflow diagram", exact: true}).click();
    await panel.waitFor({state: "detached"});
    assert.equal(await page.evaluate(() => document.body.classList.contains("has-diagram-panel")), false);

    // A template in Workflows opens today's drawing, with no version note.
    await page.getByRole("button", {name: "Workflows", exact: true}).click();
    await page.locator("[data-template-view=\"all\"]").click();
    await page.getByRole("button", {name: "Workflow diagram for Draft planned content", exact: true}).click();
    await panel.waitFor();
    assert.equal(await panel.locator("header code").innerText(), "content.generate · v1.10.0");
    await page.keyboard.press("Escape");
    await panel.waitFor({state: "detached"});

    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    server.close();
  }
});

// Every drawing in the catalog, built-in or package, in the real 420px panel.
const catalogFlows = JSON.parse(execFileSync("uv", ["run", "--frozen", "python", "-c", [
  "import json",
  "from tin_lite.catalog import BUILTIN_WORKFLOWS",
  "from tin_lite.community import discover",
  "flows = [{'key': w.key, 'flow': w.definition['presentation']['flow']} for w in BUILTIN_WORKFLOWS if w.presentation]",
  "for package in discover():",
  "    definition = json.loads((package.path / 'workflow.json').read_text())['definition']",
  "    if 'presentation' in definition:",
  "        flows.append({'key': package.key, 'flow': definition['presentation']['flow']})",
  "print(json.dumps(flows))",
].join("\n")], {encoding: "utf8"}));

test("every catalog drawing fits the panel without overlapping cards, labels or hidden lines", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html");
      return response.end(`<!doctype html><html data-theme="light"><head><link rel="stylesheet" href="/assets/app.css">
        <style>body{display:flex;flex-wrap:wrap;gap:24px;padding:24px}.workflow-diagram-panel{position:relative;inset:auto;height:auto}
        .diagram-panel-canvas{overflow:visible}</style></head><body><script src="/assets/workflow-spine.js"></script></body></html>`);
    }
    try {
      const file = path.join(assets, url.pathname.replace(/^\/assets\//, ""));
      response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
      return response.end(await fs.readFile(file));
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => document.fonts.ready);
    assert.ok(catalogFlows.length >= 66, `expected every drawing in the catalog, got ${catalogFlows.length}`);
    const problems = await page.evaluate(async (items) => {
      const found = [];
      for (const {key, flow} of items) {
        const panel = document.createElement("aside");
        panel.className = "workflow-diagram-panel";
        panel.innerHTML = '<div class="diagram-panel-canvas"></div>';
        document.body.append(panel);
        const rendered = window.TinWorkflowSpine.render(flow, {trigger: "every mon · 09:00"});
        panel.firstElementChild.append(rendered.element);
        await new Promise((resolve) => requestAnimationFrame(resolve));
        rendered.redraw();
        const box = panel.getBoundingClientRect();
        const named = (element) => `"${element.textContent.trim().slice(0, 32)}"`;
        const cardElements = [...panel.querySelectorAll(".spine-node, .spine-trigger")];
        const labelElements = [...panel.querySelectorAll(".spine-edge-label")];
        const cards = cardElements.map((node) => node.getBoundingClientRect());
        const labels = labelElements.map((label) => label.getBoundingClientRect());
        const overlaps = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
        [...cardElements, ...labelElements].forEach((element) => {
          const edge = element.getBoundingClientRect();
          const past = Math.max(box.left - edge.left, edge.right - box.right);
          if (past > 0) found.push(`${key}: ${named(element)} spills ${Math.ceil(past)}px past the panel edge`);
        });
        cards.forEach((card, index) => cards.slice(index + 1).forEach((other) => {
          if (overlaps(card, other)) found.push(`${key}: two cards overlap`);
        }));
        labels.forEach((label) => cards.forEach((card) => {
          if (overlaps(label, card)) found.push(`${key}: an edge label sits on a card`);
        }));
        // A line may only pass under the two cards it joins; a wait shows only its words.
        const solid = cardElements.map((element) => ({
          id: element.dataset.spineNode,
          element,
          box: (element.querySelector(".spine-wait-text") || element).getBoundingClientRect(),
        }));
        for (const line of panel.querySelectorAll(".spine-lines path")) {
          const matrix = line.getScreenCTM();
          const length = line.getTotalLength();
          const hidden = solid.find(({id, box}) => {
            if (id === line.dataset.from || id === line.dataset.to) return false;
            for (let at = 0; at <= length; at += 3) {
              const point = line.getPointAtLength(at).matrixTransform(matrix);
              if (point.x > box.left + 2 && point.x < box.right - 2 && point.y > box.top + 2 && point.y < box.bottom - 2) return true;
            }
            return false;
          });
          if (hidden) found.push(`${key}: the line ${line.dataset.from} to ${line.dataset.to} runs under ${named(hidden.element)}`);
        }
        const drawn = panel.querySelectorAll(".spine-lines path").length;
        if (drawn < flow.edges.length) found.push(`${key}: ${flow.edges.length - drawn} edges were not drawn`);
      }
      return found;
    }, catalogFlows);
    assert.deepEqual(problems, []);
  } finally {
    await browser.close();
    server.close();
  }
});
