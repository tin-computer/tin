// Packaged UI with synthetic APIs. No live credentials or projects.
// A browser sign-up whose project has no workflow or run yet sees the locked dashboard; a
// project with one workflow, one run, or the setting turned off, renders System as before.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
// GET /api/projects/{id}/agent-setup before the agent has done anything.
const WAITING = {agent: {connected_at: null, client: null}, setup: {run_id: null, project_id: null, project_name: null, started_at: null, status: null, plan_ready: false}};

async function serve({ lockEnabled, projectWorkflows, runs = [], connections = false, agentSetup = null }) {
  const project = {id: "project-1", name: "QA’s project", workspace_id: "ws", workspace_name: "QA", member_count: 1, hidden: false};
  // The agent's progress the lock page polls; a test moves it along by replacing `.value`.
  const setup = {value: agentSetup, polls: 0};
  const business = {...project, id: "project-acme", name: "Acme"};
  const businessRun = {id: "run-acme", workflow_id: "wf-onboarding", workflow: "growth.onboarding", status: "needs_input", created_at: "2026-10-11T21:42:00Z"};
  const integrations = connections ? [
    {key: "infra.github", name: "GitHub", badge: "GH", description: "Repositories", unlocks: [], configured: true, status: "available", connection_id: null},
    {key: "analytics.gsc", name: "Search Console", badge: "SC", description: "Search performance", unlocks: [], configured: true, status: "available", connection_id: null},
    {key: "workspace.google", name: "Google Workspace", badge: "GW", description: "Workspace", unlocks: [], configured: true, status: "available", connection_id: null},
  ] : [];
  const writes = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (["/", "/system", "/decisions", "/integrations", "/connect", "/sign-in", "/integrations/callback/github"].includes(url.pathname)) {
      response.setHeader("Content-Type", "text/html");
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8"))
        .replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "")
        .replaceAll("{{BROWSER_LOCK_ENABLED}}", String(lockEnabled)).replaceAll("{{MCP_URL}}", "https://app.tin.test/mcp")
        .replaceAll("{{AUTH_RETURN_URL}}", (url.searchParams.get("redirect_url") || "").replaceAll("&", "&amp;").replaceAll('"', "&quot;"))
        .replaceAll("{{AUTH_FLOW}}", "product");
      return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {
        const body = await fs.readFile(file);
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(body);
      } catch {response.writeHead(404).end(); return;}
    }
    if (request.method !== "GET") {
      let raw = ""; for await (const chunk of request) raw += chunk;
      writes.push({path: url.pathname, body: JSON.parse(raw || "{}")});
      if (url.pathname.endsWith("/infra.github/connect")) return send({authorization_url: "/integrations/callback/github?code=synthetic&state=synthetic"});
      if (url.pathname === "/api/integrations/github/complete") {
        Object.assign(integrations[0], {connection_id: "github-1", status: "connected", project_id: project.id, configuration: {}});
        return send(integrations[0]);
      }
      if (url.pathname.endsWith("/infra.github") && request.method === "PUT") {
        integrations[0].configuration.selected_repository = "example/site";
        return send(integrations[0]);
      }
      response.statusCode = 204; return response.end();
    }
    const planReady = Boolean(setup.value?.setup?.plan_ready);
    if (url.pathname === "/api/projects") return send(connections ? [project, {...project, id: "project-2", name: "Second project"}] : planReady ? [project, business] : [project]);
    if (url.pathname.endsWith("/agent-setup")) { setup.polls += 1; return send(setup.value || WAITING); }
    if (/\/api\/projects\/[^/]+\/workflows$/.test(url.pathname)) return send(projectWorkflows);
    if (url.pathname === `/api/projects/${business.id}/runs`) return send([businessRun]);
    if (/\/api\/projects\/[^/]+\/runs$/.test(url.pathname)) return send(runs);
    if (url.pathname.endsWith("/integrations")) return send(integrations);
    if (url.pathname.endsWith("/infra.github/options")) return send([{id: "repo-1", label: "example/site"}]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: projectWorkflows.length, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, writes, setup, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base, {viewport = {width: 1440, height: 900}, url = "/system", signedIn = true, theme = "light"} = {}) {
  const errors = [];
  const context = await browser.newContext({viewport});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(({signedIn, theme}) => {
    window.Clerk = {load: async () => {}, isSignedIn: signedIn, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", theme);
  }, {signedIn, theme});
  const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
  await page.goto(base + url);
  return {page, context, errors};
}

test("lock page: no workflow yet locks the rail and sends the person to their coding agent", async () => {
  const {server, writes, base} = await serve({lockEnabled: true, projectWorkflows: []});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, {url: "/decisions"});
    await page.locator(".lock-page").waitFor();
    assert.equal(await page.locator(".lock-page h1").textContent(), "Set up Tin from your coding agent");
    assert.equal(await page.locator(".lock-page-head p").textContent(), "Your agent reads your repo, picks the workflows that fit and starts the first ones. This page follows along.");
    assert.equal(await page.getByText("Browser setup is not available yet.").count(), 0);
    // Every rail item is dimmed and dead; the coding-agent block is gone because the page is that block.
    assert.deepEqual(await page.locator(".nav-list .nav-item").evaluateAll(items => items.map(item => item.disabled)), [true, true, true, true, true]);
    assert.equal(await page.locator("#agent-rail").isHidden(), true);
    assert.equal(await page.locator("#project-switcher").isDisabled(), false);
    // Three steps; the first holds the install line and is the only active one.
    assert.deepEqual(await page.locator(".lock-step").evaluateAll(items => items.map(item => item.className)), ["lock-step is-active", "lock-step is-pending", "lock-step is-pending"]);
    assert.deepEqual(await stepTitles(page), [
      "Run this in a terminal, inside your project",
      "Click Allow in the tab Codex opens",
      "Tin reads your project and drafts a plan",
    ]);
    assert.equal(await page.locator(".lock-page-status").textContent(), "waiting for your agent · this page updates by itself");
    // Codex first, like the website hero. The line also starts the agent with the first message.
    assert.equal(await page.locator('[data-lock-tab][aria-selected="true"]').getAttribute("data-lock-tab"), "codex");
    assert.equal(await page.locator("#lock-page-command").textContent(), 'codex mcp add tin --url https://app.tin.test/mcp && codex "Set up Tin"');
    await page.locator('[data-lock-tab="claude"]').click();
    assert.equal(await page.locator("#lock-page-command").textContent(), 'claude mcp add -s user -t http tin https://app.tin.test/mcp && claude "Set up Tin"');
    // Claude Code prints the sign-in link instead of opening a tab, then waits for a reply.
    assert.equal((await stepTitles(page))[1], "Open the link Claude Code shows and click Allow");
    assert.equal(await page.locator(".lock-step").nth(1).locator("p").textContent(), "Then tell Claude Code you're done and it carries on.");
    // One line on a desktop: the long Claude Code line is clipped with an ellipsis, never wrapped.
    const command = await page.locator("#lock-page-command").evaluate(node => ({clipped: node.scrollWidth > node.clientWidth, lines: Math.round(node.getBoundingClientRect().height / 18)}));
    assert.deepEqual(command, {clipped: true, lines: 1});
    await page.locator('[data-lock-tab="api"]').click();
    assert.equal(await page.locator("#lock-page-command").textContent(), "https://app.tin.test/api/workflows?project_id={project_id}");
    await page.locator('[data-lock-tab="codex"]').click();
    await context.grantPermissions(["clipboard-read", "clipboard-write"], {origin: base});
    await page.locator("#copy-lock-command").click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'codex mcp add tin --url https://app.tin.test/mcp && codex "Set up Tin"');
    await page.getByText("Copied. Paste it in a terminal inside your project.").waitFor();
    await page.waitForTimeout(150);
    assert.deepEqual(writes.filter(item => item.path === "/api/events/lock-page").map(item => item.body), [
      {action: "viewed", agent: null, project_id: "project-1"},
      {action: "install_copied", agent: "codex", project_id: "project-1"},
    ]);
    // Centered in the pane at desktop width, starting high so the steps can fold without a jump.
    const box = await page.locator(".lock-page-column").boundingBox();
    const pane = await page.locator("#main").boundingBox();
    assert.ok(Math.abs((box.x + box.width / 2) - (pane.x + pane.width / 2)) < 2, "column is horizontally centered");
    assert.equal(Math.round(box.width), 632);
    assert.ok(box.y > 100 && box.y + box.height < pane.height - 40, "column sits in the upper middle of the pane");
    assert.deepEqual(errors, []);
    await context.close();
    // Narrow: the rail is a top strip and the page still fits without a horizontal scroll.
    const narrow = await open(browser, base, {viewport: {width: 390, height: 844}});
    await narrow.page.locator(".lock-step").first().waitFor();
    assert.equal(await narrow.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (process.env.TIN_LOCK_SCREENSHOTS) await narrow.page.screenshot({path: path.join(process.env.TIN_LOCK_SCREENSHOTS, "lock-a-mobile.png"), fullPage: true});
    assert.deepEqual(narrow.errors, []);
    await narrow.context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

async function stepTitles(page) {
  return page.locator(".lock-step-title > span").evaluateAll(items => items.map(item => item.lastChild.textContent));
}

// The page counts a step only when it watched it happen, so a test moves the agent along
// only after the page's first poll has seen the waiting state.
async function firstPollAnswered(setup) {
  const deadline = Date.now() + 10000;
  while (setup.polls === 0) {
    if (Date.now() > deadline) throw new Error("the lock page never polled its agent setup");
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}

// Polls run every 5 s; coming back to the tab polls at once, as after the terminal.
async function pollNow(page) {
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
}

const CONNECTED = {...WAITING, agent: {connected_at: "2026-10-11T21:41:00Z", client: "claude_code"}};
const STARTED = {...CONNECTED, setup: {run_id: "run-acme", project_id: "project-acme", project_name: "Acme", started_at: "2026-10-11T21:42:00Z", status: "running", plan_ready: false}};
const READY = {...STARTED, setup: {...STARTED.setup, status: "needs_input", plan_ready: true}};

test("lock page: the steps tick as the agent connects, sets up, and the plan opens in its project", async () => {
  const {server, writes, setup, base} = await serve({lockEnabled: true, projectWorkflows: []});
  const browser = await chromium.launch({headless: true});
  const shots = process.env.TIN_LOCK_SCREENSHOTS;
  try {
    for (const theme of shots ? ["light", "dark"] : ["light"]) {
      setup.value = null;
      setup.polls = 0;
      writes.length = 0;
      const {page, context, errors} = await open(browser, base, {theme});
      await page.locator(".lock-step").first().waitFor();
      await firstPollAnswered(setup);
      if (shots) await page.screenshot({path: path.join(shots, `lock-a-${theme}.png`)});

      // B: the agent's first signed-in project call. The install line folds away.
      setup.value = CONNECTED;
      await pollNow(page);
      await page.getByText("Claude Code connected").waitFor();
      assert.deepEqual(await stepTitles(page), [
        "Tin added to Claude Code",
        "Claude Code connected",
        "Tin reads your project and drafts a plan",
      ]);
      assert.equal(await page.locator("#lock-page-command").count(), 0);
      assert.equal(await page.locator(".lock-step").nth(2).getAttribute("class"), "lock-step is-active");
      assert.ok(await page.locator(".lock-step time").textContent());

      // Setup started in the business project the agent made.
      setup.value = STARTED;
      await pollNow(page);
      await page.getByText("Tin is reading your project").waitFor();
      assert.equal(await page.locator(".lock-step").nth(2).getAttribute("class"), "lock-step is-working");
      assert.match(await page.locator(".lock-page-status").textContent(), /^started .+ · this page opens the project when the plan is ready$/);
      if (shots) await page.screenshot({path: path.join(shots, `lock-b-${theme}.png`)});

      // C: the plan waits for picks. The tab moves to that project after a short countdown.
      setup.value = READY;
      await pollNow(page);
      await page.getByText("Acme's plan is ready").waitFor();
      assert.equal(await page.locator("[data-lock-open]").textContent(), "Open Acme →");
      assert.match(await page.locator("#lock-page-countdown").textContent(), /^opening in [1-3] s$/);
      if (shots) await page.screenshot({path: path.join(shots, `lock-c-${theme}.png`)});
      await page.waitForFunction(() => document.querySelector("#project-name")?.textContent === "Acme", null, {timeout: 6000});
      await page.locator(".system-view").waitFor();
      assert.equal(await page.locator(".lock-page").count(), 0);
      assert.equal(new URL(page.url()).searchParams.get("project"), "project-acme");

      // Each step counts once, and only because this page watched it happen.
      assert.deepEqual(writes.filter(item => item.path === "/api/events/lock-page").map(item => item.body.action), ["viewed", "agent_connected", "setup_started", "plan_ready"]);
      assert.deepEqual(errors, []);
      await context.close();
    }

    // Opened once the plan is already ready: no step events, and the button switches at once.
    setup.value = READY;
    writes.length = 0;
    const {page, context, errors} = await open(browser, base);
    await page.locator("[data-lock-open]").click();
    await page.waitForFunction(() => document.querySelector("#project-name")?.textContent === "Acme");
    assert.deepEqual(writes.filter(item => item.path === "/api/events/lock-page").map(item => item.body.action), ["viewed"]);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("agent connections remain available through OAuth and repository selection without unlocking an empty project", async () => {
  const {server, writes, base} = await serve({lockEnabled: true, projectWorkflows: [], connections: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, {url: "/connect?project=project-1&providers=infra.github,analytics.gsc"});
    await page.locator(".connect-view").waitFor();
    assert.equal(await page.locator(".integration-card").count(), 2);
    assert.equal(await page.getByText("Google Workspace", {exact: true}).count(), 0);
    assert.equal(await page.locator("#agent-rail").isHidden(), true);
    assert.equal(await page.locator(".nav-list .nav-item:not(:disabled)").count(), 0);
    if (process.env.TIN_LOCK_SCREENSHOTS) {
      await page.screenshot({path: path.join(process.env.TIN_LOCK_SCREENSHOTS, "onboarding-connections-desktop.png")});
      await page.setViewportSize({width: 390, height: 844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(process.env.TIN_LOCK_SCREENSHOTS, "onboarding-connections-mobile.png"), fullPage: true});
      await page.setViewportSize({width: 1440, height: 900});
    }
    // Connect links GitHub to this project without asking which one; the sign-in returns
    // straight to the repository question.
    await page.locator('[data-integration-connect="infra.github"]').click();
    await page.getByText("example/site", {exact: true}).waitFor();
    assert.equal(await page.locator("#integration-project-title").textContent(), "Choose the website repository for QA’s project");
    assert.match(await page.locator(".connect-footer").innerText(), /0 of 2 connected/);
    await page.locator('[data-confirm-integration-project]').click();
    await page.waitForFunction(() => document.querySelector(".connect-footer")?.textContent.includes("1 of 2 connected"));
    assert.ok(writes.some(item => item.path.endsWith("/infra.github") && item.body.option_id === "repo-1"));
    await page.reload();
    await page.locator(".connect-view").waitFor();
    assert.equal(await page.locator(".integration-card").count(), 2);
    // The request is scoped to its project, including when another project is opened in this tab.
    await page.goto(base + "/integrations?project=project-2");
    await page.locator(".lock-page").waitFor();
    await page.goto(base + "/integrations?project=project-1");
    await page.locator(".connect-view").waitFor();
    await page.getByRole("button", {name: "Back to setup"}).click();
    await page.locator(".lock-page").waitFor();
    await page.reload();
    await page.locator(".lock-page").waitFor();
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("connection callbacks can finish setup in a new tab but ordinary integrations remain locked", async () => {
  const {server, base} = await serve({lockEnabled: true, projectWorkflows: [], connections: true});
  const browser = await chromium.launch({headless: true});
  try {
    for (const url of ["/integrations?project=project-1", "/connect?project=project-1&providers=unknown", "/connect?providers=infra.github"]) {
      const {page, context, errors} = await open(browser, base, {url});
      await page.locator(".lock-page").waitFor();
      assert.deepEqual(errors, []);
      await context.close();
    }
    const {page, context, errors} = await open(browser, base, {url: "/integrations/callback/github?code=synthetic&state=synthetic"});
    await page.getByText("example/site", {exact: true}).waitFor();
    assert.equal(await page.locator(".connect-view .integration-card").count(), 1);
    assert.equal(await page.locator(".nav-list .nav-item:not(:disabled)").count(), 0);
    await page.locator('[data-confirm-integration-project]').click();
    await page.waitForFunction(() => document.querySelector(".connect-footer")?.textContent.includes("All connected."));
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("agent connection links survive the sign-in return", async () => {
  const {server, base} = await serve({lockEnabled: true, projectWorkflows: [], connections: true});
  const browser = await chromium.launch({headless: true});
  try {
    const url = "/connect?project=project-1&providers=infra.github,analytics.gsc";
    const {page, context, errors} = await open(browser, base, {url, signedIn: false});
    await page.waitForURL("**/sign-in?**");
    const destination = new URL(new URL(page.url()).searchParams.get("redirect_url"));
    assert.equal(destination.origin, base);
    assert.equal(destination.pathname, "/connect");
    assert.equal(destination.searchParams.get("project"), "project-1");
    assert.equal(destination.searchParams.get("providers"), "infra.github,analytics.gsc");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("lock page: one workflow, one run, or the setting off, renders System as before", async () => {
  const workflow = {id: "cfg-1", workflow_id: "wf-1", name: "Audit AI visibility", status: "active", schedule: null, inputs: {}};
  // A one-off run started by the coding agent, with no saved workflow.
  const run = {id: "run-1", workflow_id: "wf-1", workflow: "organic.audit", status: "succeeded", created_at: "2026-09-01T00:00:00Z"};
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of [{lockEnabled: true, projectWorkflows: [workflow]}, {lockEnabled: true, projectWorkflows: [], runs: [run]}, {lockEnabled: false, projectWorkflows: []}]) {
      const {server, writes, base} = await serve(scenario);
      try {
        const {page, context, errors} = await open(browser, base);
        await page.locator(".system-view").waitFor();
        assert.equal(await page.locator(".lock-page").count(), 0);
        assert.equal(await page.locator("#agent-rail").isVisible(), true);
        assert.deepEqual(await page.locator(".nav-list .nav-item").evaluateAll(items => items.map(item => item.disabled)), [false, false, false, false, false]);
        assert.deepEqual(writes.filter(item => item.path === "/api/events/lock-page"), []);
        assert.deepEqual(errors, []);
        await context.close();
      } finally {
        server.close();
      }
    }
  } finally {
    await browser.close();
  }
});
