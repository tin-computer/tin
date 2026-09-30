import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

test("X integration shows account and capabilities, upgrades without resource options, and disconnects", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id: "project-one", name: "Tin", workspace_id: "workspace", workspace_name: "Tin", can_create_project_in_workspace: true, member_count: 1, hidden: false};
  let connection = true;
  const calls = [];
  const xIntegration = () => ({
    key: "social.x", name: "X", badge: "X", description: "Read your own posts and publish only posts you explicitly approve.",
    unlocks: ["X writing voice", "Approved X posts"], configured: true,
    connection_id: connection ? "connection-one" : null, status: connection ? "connected" : "available",
    external_account_label: connection ? "@tin" : null, connected_at: "2026-09-29T00:00:00Z", last_checked_at: "2026-09-29T01:00:00Z",
    configuration: {granted_capabilities: ["x.posts.read"]},
  });
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (url.pathname === "/" || url.pathname === "/integrations") {
      response.setHeader("Content-Type", "text/html");
      return response.end((await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "").replaceAll("{{BILLING_ENABLED}}", "false"));
    }
    if (url.pathname === "/authorize") return response.end("Authorized");
    if (url.pathname.startsWith("/assets/")) {
      try {
        const file = path.join(assets, url.pathname.slice(8));
        const body = await fs.readFile(file);
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(body);
      } catch {response.writeHead(404).end(); return;}
    }
    if (request.method === "POST" || request.method === "DELETE") {
      let body = ""; for await (const chunk of request) body += chunk;
      calls.push({method: request.method, path: url.pathname, body: body ? JSON.parse(body) : null});
      if (request.method === "DELETE") {connection = false; response.writeHead(204).end(); return;}
      return send({authorization_url: `http://127.0.0.1:${server.address().port}/authorize`});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname.endsWith("/integrations")) return send([xIntegration()]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.endsWith("/options")) {calls.push({method: "GET", path: url.pathname}); return send([]);}
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}};
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/integrations?project=project-one`);
    const card = page.locator(".integration-card").filter({hasText: "social.x"});
    await card.waitFor();
    assert.match(await card.innerText(), /@tin[\s\S]*reading only/);
    await card.getByRole("button", {name: "Configure"}).click();
    assert.match(await card.innerText(), /Read your own posts[\s\S]*Publish posts you approve[\s\S]*Upload media/);
    assert.equal(calls.filter(call => call.path.endsWith("/options")).length, 0);
    await card.getByRole("button", {name: "Enable publishing"}).click();
    await page.waitForURL(`${base}/authorize`);
    assert.deepEqual(calls.filter(call => call.method === "POST").map(call => call.body), [{capabilities: ["x.posts.publish"]}]);
    await page.goto(`${base}/integrations?project=project-one`);
    await card.waitFor();
    await card.getByRole("button", {name: "Configure"}).click();
    page.once("dialog", dialog => dialog.accept());
    await card.getByRole("button", {name: "Disconnect"}).click();
    await card.getByRole("button", {name: "Connect"}).waitFor();
    assert.equal(calls.filter(call => call.method === "DELETE").length, 1);
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});

test("X OAuth callback exchanges state and code, then returns to the project integration page", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const calls = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (url.pathname === "/integrations/callback/x" || url.pathname === "/integrations") {
      response.setHeader("Content-Type", "text/html");
      return response.end((await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "").replaceAll("{{BILLING_ENABLED}}", "false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      try {
        const file = path.join(assets, url.pathname.slice(8));
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(await fs.readFile(file));
      } catch {response.writeHead(404).end(); return;}
    }
    if (url.pathname === "/api/integrations/x/complete" && request.method === "POST") {
      let body = ""; for await (const chunk of request) body += chunk;
      calls.push(JSON.parse(body));
      return send({key: "social.x", name: "X", project_id: "project-one"});
    }
    if (url.pathname === "/api/projects") return send([{id: "project-one", name: "Tin", workspace_id: "workspace", workspace_name: "Tin", member_count: 1, hidden: false}]);
    if (url.pathname.endsWith("/integrations")) return send([{key: "social.x", name: "X", badge: "X", configured: true, connection_id: "connection-one", status: "connected", external_account_label: "@tin", configuration: {granted_capabilities: ["x.posts.read"]}}]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}};});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/integrations/callback/x?state=bound-state&code=one-use-code`);
    await page.waitForURL(`${base}/integrations?project=project-one`);
    assert.deepEqual(calls, [{state: "bound-state", code: "one-use-code"}]);
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});

test("MCP X draft handoff opens the composer for its project without exposing raw media", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const calls = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (url.pathname === "/activity") {
      response.setHeader("Content-Type", "text/html");
      return response.end((await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "").replaceAll("{{BILLING_ENABLED}}", "false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      try {
        const file = path.join(assets, url.pathname.slice(8));
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(await fs.readFile(file));
      } catch {response.writeHead(404).end(); return;}
    }
    if (url.pathname === "/api/projects/project-one/x/drafts") {
      calls.push(url.searchParams.get("path"));
      return send({path: "social/x/draft.json", revision: "a".repeat(40), draft: {
        schema_version: "tin.social.x_draft.v1", account_id: "123", posts: [{id: "p1", text: "A saved post", readiness: "ready", support: [], editor_notes: "", attachments: [], missing_assets: []}],
      }});
    }
    if (url.pathname === "/api/projects") return send([{id: "project-one", name: "Tin", workspace_id: "workspace", workspace_name: "Tin", member_count: 1, hidden: false}]);
    if (url.pathname.endsWith("/integrations")) return send([]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 0, runs_this_month: 0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}};});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/activity?project=project-one&x_draft=social%2Fx%2Fdraft.json`);
    await page.locator('[data-x-text="0"]').waitFor();
    assert.equal(await page.locator('[data-x-text="0"]').inputValue(), "A saved post");
    assert.deepEqual(calls, ["social/x/draft.json"]);
    assert.equal(new URL(page.url()).searchParams.has("x_draft"), false);
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});
