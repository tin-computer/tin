import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

for (const width of [1120, 390]) test(`X feedback at ${width}px relays verbatim, remembers automatically and never publishes`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width, height: 900}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("http://localhost/x-feedback", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-feedback");
    for (const file of ["app.css", "workflow-review.css", "x-posts.css"]) await page.addStyleTag({path: `src/tin_lite/static/${file}`});
    for (const file of ["markdown-viewer.js", "x-posts.js"]) await page.addScriptTag({path: `src/tin_lite/static/${file}`});
    await page.evaluate(() => {
      window.requests = []; window.done = false; window.failFirst = true;
      const post = {id: "p1", text: "we shipped a demo.", readiness: "ready", support: [], editor_notes: "", attachments: [], missing_assets: []};
      window.TinXPosts.open({projectId: "project-one", path: "social/x/draft.json", api: async (path, options = {}) => {
        const body = options.body && JSON.parse(options.body); requests.push({path, body});
        if (path.includes("/x/drafts")) return {feedback_run_id: "source", sha256: "a".repeat(64), revision: "a".repeat(40), draft: {schema_version: "tin.social.x_draft.v1", account_id: "123", posts: [{...post, text: done ? "try the demo." : post.text}]}};
        if (path.includes("/review?")) return {run_id: "source", review_token: "b".repeat(64), artifact: {sha256: (window.stale ? "b" : "a").repeat(64)}, can_request_changes: true,
          change_summary: done ? "Shortened the post. Remembered for future X posts: Lead product updates with a demo." : null,
          feedback_hint: "Tin carries clear writing preferences into your X guide. One-off changes stay with this draft."};
        if (path.endsWith("/request-changes")) {
          if (failFirst) {failFirst = false; throw new Error("Response lost. Retry the same request.");}
          return {id: "revision", status: "pending"};
        }
        if (path.endsWith("/revision")) {done = true; return {id: "revision", status: "succeeded"};}
        throw new Error(`Unexpected ${path}`);
      }});
    });
    await page.getByRole("button", {name: "Request changes", exact: true}).click();
    const feedback = "  For product updates, lead with the demo.\nRemove the second sentence.  ";
    await page.getByLabel("What should change?").fill(feedback);
    assert.equal(await page.getByRole("checkbox").count(), 0);
    assert.equal(await page.locator(".x-posts-copy").textContent(), "we shipped a demo.");
    await page.getByRole("button", {name: "Revise draft", exact: true}).click();
    await page.getByText("Response lost. Retry the same request.", {exact: true}).waitFor();
    assert.equal(await page.getByLabel("What should change?").inputValue(), feedback);
    await page.getByRole("button", {name: "Revise draft", exact: true}).click();
    await page.getByText("try the demo.", {exact: true}).waitFor();
    await page.getByText(/Remembered for future X posts/).waitFor();
    const requests = await page.evaluate(() => window.requests);
    const changes = requests.filter(r => r.path.endsWith("request-changes"));
    assert.equal(changes.length, 2);
    assert.deepEqual(changes[0].body, changes[1].body);
    assert.equal(changes[0].body.feedback, feedback);
    assert.equal(changes[0].body.post_id, "p1");
    assert.equal(requests.some(r => r.path.includes("/publish") || r.path.includes("/approve")), false);
    await page.evaluate(() => {window.stale = true;});
    await page.getByRole("button", {name: "Reload saved draft", exact: true}).click();
    await page.getByText("This draft changed in Files. Reload the saved draft before giving feedback.", {exact: true}).waitFor();
    assert.equal(await page.getByRole("button", {name: "Request changes", exact: true}).count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});

test("X guide reader refreshes cached text before enabling its new approval token", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id: "project", name: "Example", workspace_id: "workspace", workspace_name: "Example", timezone: "UTC", member_count: 1};
  const workflow = {id: "style", key: "social.x_style", title: "Learn my X writing style", executor: "social.x_style", status: "active", definition: {human_review: {eligible: true}, input_schema: {type: "object", properties: {}}}};
  const run = {id: "guide", project_id: project.id, workflow_id: workflow.id, workflow_name: workflow.key, status: "needs_input", review_required: true, artifact_path: "style/proposals/x.md", canonical_commit_sha: "a".repeat(40), created_at: "2026-09-30T12:00:00Z"};
  let copy = "a", reads = 0, releaseDocument, holdDocument = false;
  const writes = [], errors = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|document\/[^/]+)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "");
      response.setHeader("Content-Type", "text/html"); return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const body = await fs.readFile(file); response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"); return response.end(body);} catch {response.writeHead(404); return response.end();}
    }
    if (request.method === "POST") {
      let text = ""; for await (const chunk of request) text += chunk;
      writes.push({path: url.pathname, body: JSON.parse(text || "{}")});
      return send({...run, status: "running"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/workflows/style") return send(workflow);
    if (url.pathname.endsWith("/runs")) return send([run]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 1, runs_this_month: 1});
    if (url.pathname.endsWith("/review")) return send({run_id: run.id, current_run_id: run.id, status: run.status, version: 1, is_current: true, x_feedback: true, can_request_changes: true, can_approve: true, review_token: copy.repeat(64), artifact: {sha256: copy.repeat(64)}});
    if (url.pathname.endsWith("/review/document") || url.pathname.endsWith("/artifact/document")) {
      reads++;
      if (holdDocument) await new Promise(resolve => {releaseDocument = resolve;});
      return send({filename: "x.md", path: run.artifact_path, revision: copy.repeat(40), sha256: copy.repeat(64), markdown: `# Guide ${copy}`, html: `<h1>Guide ${copy}</h1><p>Current writing preferences.</p>`, word_count: 6, reading_minutes: 1, headings: []});
    }
    if (url.pathname === "/api/workflows/runs/guide") return send(run);
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member"}, session: {getToken: async () => "synthetic"}};});
    const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/document/guide?project=project`);
    await page.getByRole("heading", {name: "Guide a", exact: true}).waitFor();
    await page.waitForFunction(() => TinWorkflowReview.token("project", "guide") === "a".repeat(64));
    const firstReads = reads;
    // A Files edit changes the content before the original run projection catches up.
    copy = "b"; holdDocument = true;
    await page.evaluate(() => {navigate("workflows"); openDocument("guide");});
    await page.waitForFunction(() => !TinWorkflowReview.token("project", "guide"));
    assert.equal(await page.locator(".markdown-context-action:not(.is-secondary):enabled:visible").count(), 0);
    const deadline = Date.now() + 3000;
    while (!releaseDocument && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(releaseDocument, "Reader should fetch the updated guide");
    holdDocument = false; releaseDocument();
    await page.getByRole("heading", {name: "Guide b", exact: true}).waitFor();
    await page.waitForFunction(() => TinWorkflowReview.token("project", "guide") === "b".repeat(64));
    assert.equal(reads, firstReads + 1);
    // An ordinary reread reuses the now-matching cached document.
    await page.evaluate(() => {navigate("workflows"); openDocument("guide");});
    await page.waitForFunction(() => TinWorkflowReview.token("project", "guide") === "b".repeat(64));
    assert.equal(reads, firstReads + 1);
    // Feedback updates the same source run and its canonical artifact revision.
    copy = "c"; run.canonical_commit_sha = "c".repeat(40);
    await page.evaluate(value => {upsertRun(value); render();}, run);
    await page.getByRole("heading", {name: "Guide c", exact: true}).waitFor();
    await page.waitForFunction(() => TinWorkflowReview.token("project", "guide") === "c".repeat(64));
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith("/approve")),
      page.getByRole("button", {name: "Approve draft", exact: true}).click(),
    ]);
    assert.equal(writes.find(item => item.path.endsWith("/approve"))?.body.review_token, "c".repeat(64));
    assert.deepEqual(errors, []);
  } finally {releaseDocument?.(); await browser.close(); await new Promise(resolve => server.close(resolve));}
});
