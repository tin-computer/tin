// Packaged UI with synthetic APIs. No live credentials or projects.
// A browser sign-up whose project has no workflow yet sees the locked dashboard; a project
// with one workflow, or the setting turned off, renders System as before.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");

async function serve({ lockEnabled, projectWorkflows, connections = false }) {
  const project = {id: "project-1", name: "QA’s project", workspace_id: "ws", workspace_name: "QA", member_count: 1, hidden: false};
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
    if (url.pathname === "/api/projects") return send(connections ? [project, {...project, id: "project-2", name: "Second project"}] : [project]);
    if (/\/api\/projects\/[^/]+\/workflows$/.test(url.pathname)) return send(projectWorkflows);
    if (url.pathname.endsWith("/integrations")) return send(integrations);
    if (url.pathname.endsWith("/infra.github/options")) return send([{id: "repo-1", label: "example/site"}]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: projectWorkflows.length, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, writes, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base, {viewport = {width: 1440, height: 900}, url = "/system", signedIn = true} = {}) {
  const errors = [];
  const context = await browser.newContext({viewport});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(signedIn => {
    window.Clerk = {load: async () => {}, isSignedIn: signedIn, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", "light");
  }, signedIn);
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
    assert.equal(await page.locator(".lock-page p strong").textContent(), "Browser setup is not available yet.");
    // Every rail item is dimmed and dead; the coding-agent block is gone because the page is that block.
    assert.deepEqual(await page.locator(".nav-list .nav-item").evaluateAll(items => items.map(item => item.disabled)), [true, true, true, true, true]);
    assert.equal(await page.locator("#agent-rail").isHidden(), true);
    assert.equal(await page.locator("#project-switcher").isDisabled(), false);
    // Codex first, like the website hero; one line, no comment lines.
    assert.equal(await page.locator('[data-lock-tab][aria-selected="true"]').getAttribute("data-lock-tab"), "codex");
    assert.equal(await page.locator("#lock-page-command").textContent(), "codex mcp add tin --url https://app.tin.test/mcp");
    await page.locator('[data-lock-tab="claude"]').click();
    assert.equal(await page.locator("#lock-page-command").textContent(), "claude mcp add -t http tin https://app.tin.test/mcp");
    await page.locator('[data-lock-tab="api"]').click();
    assert.equal(await page.locator("#lock-page-command").textContent(), "https://app.tin.test/api/workflows?project_id={project_id}");
    await page.locator('[data-lock-tab="codex"]').click();
    await context.grantPermissions(["clipboard-read", "clipboard-write"], {origin: base});
    await page.locator("#copy-lock-command").click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "codex mcp add tin --url https://app.tin.test/mcp");
    await page.waitForTimeout(150);
    assert.deepEqual(writes.map(item => item.body), [
      {action: "viewed", agent: null, project_id: "project-1"},
      {action: "install_copied", agent: "codex", project_id: "project-1"},
    ]);
    // Centered in the pane at desktop width.
    const box = await page.locator(".lock-page-column").boundingBox();
    const pane = await page.locator("#main").boundingBox();
    assert.ok(Math.abs((box.x + box.width / 2) - (pane.x + pane.width / 2)) < 2, "column is horizontally centered");
    assert.ok(box.y > 150 && box.y + box.height < pane.height - 150, "column sits in the middle of the pane");
    assert.deepEqual(errors, []);
    await context.close();
    // Narrow: the rail is a top strip and the page still fits without a horizontal scroll.
    const narrow = await open(browser, base, {viewport: {width: 390, height: 844}});
    await narrow.page.locator(".lock-page").waitFor();
    assert.equal(await narrow.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.deepEqual(narrow.errors, []);
    await narrow.context.close();
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
    await page.locator('[data-integration-connect="infra.github"]').click();
    await page.locator('[data-confirm-integration-project]').click();
    await page.getByText("example/site", {exact: true}).waitFor();
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

test("lock page: one workflow, or the setting off, renders System as before", async () => {
  const workflow = {id: "cfg-1", workflow_id: "wf-1", name: "Audit AI visibility", status: "active", schedule: null, inputs: {}};
  const browser = await chromium.launch({headless: true});
  try {
    for (const scenario of [{lockEnabled: true, projectWorkflows: [workflow]}, {lockEnabled: false, projectWorkflows: []}]) {
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
