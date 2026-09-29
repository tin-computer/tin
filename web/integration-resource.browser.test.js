// Packaged dashboard, synthetic HTTP only. No live credentials or projects.
// Connect links a service to the project you are in. Right after its sign-in returns, one
// dialog asks which repository, Search Console property or PostHog project to link; "Finish
// setup" and "Configure" open the same dialog, and "Later" keeps the connection unfinished.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");

const SERVICES = {
  "infra.github": {
    name: "GitHub", callback: "/integrations/callback/github?code=synthetic&state=synthetic&installation_id=7&setup_action=install",
    complete: "/api/integrations/github/complete", selection: "selected_repository",
    title: "Choose the repository for Example project",
    copy: "Tin opens pull requests and delivers approved pages here.",
    confirm: "Link repository", options: [{id: "example/site", label: "example/site"}, {id: "example/docs", label: "example/docs"}],
  },
  "analytics.gsc": {
    name: "Google Search Console", callback: "/integrations/callback/google?code=synthetic&state=synthetic",
    complete: "/api/integrations/google/complete", selection: "selected_site_url",
    title: "Choose the Search Console property for Example project",
    copy: "Tin reads real searches, clicks and positions from this property.",
    confirm: "Link property", options: [{id: "sc-domain:example.com", label: "sc-domain:example.com"}, {id: "https://www.example.com/", label: "https://www.example.com/"}],
  },
  "analytics.posthog": {
    name: "PostHog", callback: "/integrations/callback/posthog?code=synthetic&state=synthetic",
    complete: "/api/integrations/posthog/complete", selection: "selected_project_id",
    title: "Choose the PostHog project for Example project",
    copy: "Tin reads events and funnels from this one project, read only.",
    confirm: "Link project", options: [{id: "101", label: "Example app", detail: "US Cloud"}, {id: "202", label: "Example docs", detail: "US Cloud"}],
  },
};

async function serve({connected = false} = {}) {
  const project = {id: "project-1", name: "Example project", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const integrations = Object.entries(SERVICES).map(([key, service]) => ({
    key, name: service.name, badge: service.name.slice(0, 2), description: service.name, unlocks: ["workflows"],
    configured: true, status: connected ? "connected" : "available", connection_id: connected ? `${key}-1` : null,
    project_id: project.id, configuration: {},
  }));
  const writes = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|integrations|integrations\/callback\/[a-z-]+)$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "");
      response.setHeader("Content-Type", "text/html"); return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const body = await fs.readFile(file); response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".svg") ? "image/svg+xml" : "application/octet-stream"); return response.end(body);} catch {response.writeHead(404); return response.end();}
    }
    const byKey = key => integrations.find(item => item.key === key);
    if (request.method !== "GET") {
      let raw = ""; for await (const chunk of request) raw += chunk;
      const body = JSON.parse(raw || "{}");
      writes.push({method: request.method, path: url.pathname, body});
      const connect = url.pathname.match(/\/integrations\/([^/]+)\/connect$/);
      if (connect) return send({authorization_url: SERVICES[connect[1]].callback});
      const completed = Object.entries(SERVICES).find(([, service]) => service.complete === url.pathname);
      if (completed) {
        Object.assign(byKey(completed[0]), {connection_id: `${completed[0]}-1`, status: "connected", configuration: {}});
        return send(byKey(completed[0]));
      }
      const chosen = url.pathname.match(/\/integrations\/([^/]+)$/);
      if (chosen && request.method === "PUT") {
        const service = SERVICES[chosen[1]];
        const option = service.options.find(item => item.id === body.option_id);
        Object.assign(byKey(chosen[1]), {configuration: {[service.selection]: option.id}, external_account_label: option.label});
        return send(byKey(chosen[1]));
      }
      return send({});
    }
    if (url.pathname === "/api/projects") return send([project, {...project, id: "project-2", name: "Second project"}]);
    const options = url.pathname.match(/\/integrations\/([^/]+)\/options$/);
    if (options) return send(SERVICES[options[1]].options);
    if (url.pathname.endsWith("/integrations")) return send(integrations);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, waiting_count: 0, runs_this_month: 1, timezone: "UTC"});
    if (/\/api\/projects\/[^/]+\/workflows$/.test(url.pathname)) return send([{id: "saved", workflow_key: "research.deep_dive", status: "active"}]);
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, writes, integrations, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base, url) {
  const errors = [];
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(() => {
    window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic-test-only"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", "light");
    localStorage.setItem("tin-lite:project", "project-1");
  });
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(base + url);
  return {page, context, errors};
}

async function dialogText(page) {
  const dialog = page.locator("#integration-project-dialog");
  await dialog.locator('[role="radio"]').first().waitFor();
  return {
    title: await dialog.locator("h2").textContent(),
    copy: await dialog.locator("#integration-project-copy").textContent(),
    options: await dialog.locator('[role="radio"]').allInnerTexts(),
    checked: await dialog.locator('[role="radio"][aria-checked="true"]').innerText(),
    buttons: await dialog.locator("footer button").allTextContents(),
  };
}

for (const [key, service] of Object.entries(SERVICES)) {
  test(`${service.name}: Connect links this project, then asks which ${service.confirm.split(" ")[1]} to link`, async () => {
    const {server, writes, base} = await serve();
    const browser = await chromium.launch({headless: true});
    try {
      const {page, context, errors} = await open(browser, base, "/integrations?project=project-1");
      await page.locator(`[data-integration-connect="${key}"]`).click();
      // No project question: the sign-in starts at once for the project you are in, and
      // one dialog asks the one question right after it returns.
      const shown = await dialogText(page);
      assert.deepEqual(writes.filter(item => item.path.endsWith("/connect")).map(item => item.path), [`/api/projects/project-1/integrations/${key}/connect`]);
      assert.equal(new URL(page.url()).pathname, "/integrations");
      assert.equal(shown.title, service.title);
      assert.equal(shown.copy, service.copy);
      assert.deepEqual(shown.options.map(item => item.replace(/\s+/g, " ").trim()), service.options.map(item => [item.label, item.detail].filter(Boolean).join(" ")));
      assert.deepEqual(shown.buttons, ["Later", service.confirm]);
      await page.locator('[role="radio"]').nth(1).click();
      await page.getByRole("button", {name: service.confirm, exact: true}).click();
      await page.getByText(`${service.name} now uses ${service.options[1].label}.`, {exact: true}).waitFor();
      assert.deepEqual(writes.filter(item => item.method === "PUT").map(item => [item.path, item.body]), [[`/api/projects/project-1/integrations/${key}`, {option_id: service.options[1].id}]]);
      assert.equal(await page.locator("#integration-project-dialog").isVisible(), false);
      assert.equal(await page.locator(`[data-integration-choose="${key}"]`).textContent(), "Configure");
      assert.deepEqual(errors, []);
      await context.close();
    } finally { await browser.close(); server.close(); }
  });
}

test("Finish setup opens the same dialog; Later keeps the connection unfinished", async () => {
  const {server, writes, base} = await serve({connected: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/integrations?project=project-1");
    for (const [key, service] of Object.entries(SERVICES)) {
      const row = page.locator(`[data-integration-choose="${key}"]`);
      assert.equal(await row.textContent(), "Finish setup");
      await row.click();
      const shown = await dialogText(page);
      assert.equal(shown.title, service.title);
      assert.deepEqual(shown.buttons, ["Later", service.confirm]);
      // No inline picker: the row's details stay read-only underneath.
      assert.equal(await page.locator(".integration-card .integration-config-form, .integration-card .tin-select").count(), 0);
      await page.getByRole("button", {name: "Later", exact: true}).click();
      assert.equal(await page.locator("#integration-project-dialog").isVisible(), false);
      assert.equal(await page.locator(`[data-integration-choose="${key}"]`).textContent(), "Finish setup");
      assert.equal(writes.filter(item => item.method === "PUT" && item.path.endsWith(`/${key}`)).length, 0);

      await page.locator(`[data-integration-choose="${key}"]`).click();
      await dialogText(page);
      await page.getByRole("button", {name: service.confirm, exact: true}).click();
      await page.getByText(`${service.name} now uses ${service.options[0].label}.`, {exact: true}).waitFor();
      assert.equal(await page.locator(`[data-integration-choose="${key}"]`).textContent(), "Configure");

      // Configure asks again, starting from the current choice.
      await page.locator(`[data-integration-choose="${key}"]`).click();
      assert.equal((await dialogText(page)).checked.split("\n")[0], service.options[0].label);
      await page.getByRole("button", {name: "Later", exact: true}).click();
    }
    // Another dialog that reuses the same element keeps its own Cancel button.
    await page.waitForFunction(() => document.querySelector("[data-cancel-integration-project]").textContent === "Cancel");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});
