import assert from "node:assert/strict";
import test from "node:test";
import {chromium} from "playwright";

const initial = {
  schema_version: "tin.social.x_draft.v1", account_id: "123",
  posts: [
    {id: "p1", text: "A factual post.", readiness: "ready", support: [{source_path: "notes/release.md", excerpt: "Shipped."}], editor_notes: "Keep it clear.", attachments: [], missing_assets: []},
    {id: "p2", text: "Another angle.", readiness: "needs_asset", support: [], editor_notes: "", attachments: [], missing_assets: ["Screenshot"]},
  ],
};

test("X composer saves edits, pins media, previews exact post, and sends one explicit publish", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 760, height: 900}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("http://localhost/x-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-post-test");
    await page.addStyleTag({path: "src/tin_lite/static/app.css"});
    await page.addStyleTag({path: "src/tin_lite/static/x-posts.css"});
    await page.addScriptTag({path: "src/tin_lite/static/markdown-viewer.js"});
    await page.addScriptTag({path: "src/tin_lite/static/x-posts.js"});
    await page.evaluate(draft => {
      window.requests = [];
      window.saved = structuredClone(draft);
      window.revision = "a".repeat(40);
      window.published = 0;
      window.mockApi = async (path, options = {}) => {
        const body = options.body && JSON.parse(options.body);
        requests.push({path, method: options.method || "GET", body});
        if (path.endsWith("/files")) return {files: [{path: "images/first.png"}, {path: "images/second.png"}]};
        if (path.includes("/x/drafts") && !options.method) return {path: "social/x/draft.json", revision, draft: structuredClone(saved)};
        if (path.endsWith("/x/drafts") && options.method === "PUT") {
          if (body.expected_revision !== revision) throw new Error("Project file changed. Reload.");
          saved = structuredClone(body.draft); revision = "b".repeat(40);
          return {path: body.path, revision, draft: structuredClone(saved)};
        }
        if (path.endsWith("/x/preview")) return {preview_token: "exact-token", text: saved.posts.find(post => post.id === body.post_id).text, account: {id: "123", username: "@tin"}, attachments: saved.posts.find(post => post.id === body.post_id).attachments, valid_until: "2026-09-30T00:00:00Z"};
        if (path.endsWith("/x/publish")) {published++; return {id: "delivery-id", run_id: "delivery-id", status: "pending"};}
        throw new Error(`Unexpected ${path}`);
      };
      window.mockRawFetch = async (path, options = {}) => {
        requests.push({path, method: options.method || "GET", type: options.body?.type});
        if (options.method === "POST") return new Response(JSON.stringify({path: new URL(path, location.href).searchParams.get("path"), revision: "c".repeat(40), media_type: options.body.type, bytes: options.body.size}), {headers: {"Content-Type": "application/json"}});
        return new Response(new Blob(["file"], {type: "image/png"}));
      };
      TinXPosts.open({projectId: "project-one", runId: "run-one", path: "social/x/draft.json", api: mockApi, rawFetch: mockRawFetch, onPublished: async () => {window.refreshed = true;}});
    }, initial);
    await page.locator(".x-posts-card").first().waitFor();
    assert.equal(await page.locator(".markdown-viewer .markdown-document.x-posts-document").count(), 1);
    assert.equal(await page.locator(".x-posts-card").count(), 1);
    assert.equal(await page.locator(".x-posts-nav button").count(), 2);
    assert.equal(await page.locator("dialog").count(), 0);
    if (process.env.TIN_X_SCREENSHOTS) {
      await page.setViewportSize({width: 1120, height: 960});
      await page.screenshot({path: "/tmp/tin-x-workflow-desktop.png", fullPage: true});
      await page.setViewportSize({width: 390, height: 844});
      await page.screenshot({path: "/tmp/tin-x-workflow-narrow.png", fullPage: true});
      await page.setViewportSize({width: 760, height: 900});
    }
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    const revisedText = "The exact revised post.\n**Literal** #tag <demo>";
    await page.locator('[data-x-text="0"]').fill(revisedText);
    await page.locator('[data-x-action="preview"][data-post="0"]').click();
    await page.locator("[data-x-exact]").waitFor();
    assert.match(await page.locator("[data-x-exact]").innerText(), /@tin[\s\S]*The exact revised post/);
    assert.equal(await page.locator("[data-x-exact] .x-posts-copy").textContent(), revisedText);
    assert.equal(await page.locator(".x-posts-copy strong, .x-posts-copy demo").count(), 0);
    let calls = await page.evaluate(() => requests.filter(item => ["PUT", "POST"].includes(item.method)));
    assert.deepEqual(calls.map(item => item.path.split("/").at(-1)), ["drafts", "preview"]);
    assert.equal(calls[0].body.expected_revision, "a".repeat(40));
    assert.equal(calls[0].body.draft.posts[1].readiness, "needs_asset");
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator('[data-x-text="0"]').fill("Changed after preview.");
    assert.equal(await page.locator("[data-x-exact]").count(), 0);
    assert.equal(await page.locator('[data-x-action="publish"]').count(), 0);
    await page.locator('[data-x-action="preview"][data-post="0"]').click();
    await page.locator('[data-x-action="publish"]').click();
    await page.getByText(/Follow its progress in Activity/).waitFor();
    calls = await page.evaluate(() => requests.filter(item => item.path.endsWith("/x/publish")));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].body.preview_token, "exact-token");
    assert.equal(await page.evaluate(() => refreshed), true);
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});

test("X composer uploads bounded media through project Files and keeps unsaved edits on stale revision", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route("http://localhost/x-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-post-test");
    await page.addScriptTag({path: "src/tin_lite/static/markdown-viewer.js"});
    await page.addScriptTag({path: "src/tin_lite/static/x-posts.js"});
    await page.evaluate(draft => {
      window.calls = [];
      TinXPosts.open({projectId: "project-two", runId: "run-two", path: "social/x/draft.json", supportsAltText: true, api: async (path, options = {}) => {
        calls.push({path, body: options.body && JSON.parse(options.body)});
        if (!options.method) return {revision: "a".repeat(40), draft};
        throw new Error("Project file changed. Reload.");
      }, rawFetch: async (path, options = {}) => {
        calls.push({path, type: options.body?.type});
        if (options.method === "POST") return new Response(JSON.stringify({path: new URL(path, location.href).searchParams.get("path"), revision: "b".repeat(40), media_type: options.body.type, bytes: options.body.size}));
        return new Response(new Blob(["image"], {type: "image/png"}));
      }});
    }, initial);
    await page.locator(".x-posts-card").first().waitFor();
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator('[data-x-upload="0"]').setInputFiles({name: "demo.png", mimeType: "image/png", buffer: Buffer.from([137, 80, 78, 71])});
    await page.getByText(/Media uploaded to Files/).waitFor();
    await page.locator('[data-x-alt="0:0"]').fill("Screenshot of the new flow");
    await page.locator('[data-x-action="save"]').first().click();
    await page.getByText(/Your edits are still here/).waitFor();
    assert.equal(await page.locator('[data-x-alt="0:0"]').inputValue(), "Screenshot of the new flow");
    const calls = await page.evaluate(() => window.calls);
    const upload = calls.find(item => item.type === "image/png");
    assert.match(upload.path, /project-two\/files\/upload\?/);
    assert.match(upload.path, /expected_revision=a{40}/);
    const save = calls.find(item => item.body?.draft);
    assert.equal(save.body.expected_revision, "b".repeat(40));
    assert.equal(save.body.draft.posts[0].attachments[0].alt_text, "Screenshot of the new flow");
    const uploadsBefore = calls.filter(item => item.type === "image/png").length;
    await page.locator('[data-x-upload="0"]').setInputFiles({name: "oversize.png", mimeType: "image/png", buffer: Buffer.alloc(5_000_001)});
    await page.getByText(/no larger than 5 MB/).waitFor();
    assert.equal(await page.evaluate(() => calls.filter(item => item.type === "image/png").length), uploadsBefore);
  } finally {await browser.close();}
});

test("X attachment order invalidates the exact preview and an uncertain publish only retries on an explicit click", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route("http://localhost/x-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-post-test");
    await page.addScriptTag({path: "src/tin_lite/static/markdown-viewer.js"});
    await page.addScriptTag({path: "src/tin_lite/static/x-posts.js"});
    await page.evaluate(draft => {
      window.calls = [];
      window.saved = structuredClone(draft);
      window.revision = "a".repeat(40);
      window.publishes = 0;
      TinXPosts.open({projectId: "project-one", runId: "run-one", path: "social/x/draft.json", api: async (path, options = {}) => {
        const body = options.body && JSON.parse(options.body);
        calls.push({path, body});
        if (path.endsWith("/files")) return {files: [{path: "images/first.png"}, {path: "images/second.png"}]};
        if (path.includes("/x/drafts") && !options.method) return {revision, draft: structuredClone(saved)};
        if (path.endsWith("/x/drafts")) {saved = structuredClone(body.draft); revision = "b".repeat(40); return {revision, draft: structuredClone(saved)};}
        if (path.endsWith("/x/preview")) return {preview_token: "token", text: saved.posts[0].text, account: {id: "123", username: "tin"}, attachments: saved.posts[0].attachments.map(item => ({...item, url: `/api/projects/project-one/files/raw?path=${encodeURIComponent(item.path)}&revision=${revision}`}))};
        if (path.endsWith("/x/publish")) {publishes++; throw new Error("Connection lost after dispatch");}
        throw new Error(path);
      }, rawFetch: async () => new Response(new Blob(["image"], {type: "image/png"}))});
    }, initial);
    await page.locator(".x-posts-card").first().waitFor();
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByRole("button", {name: "Choose from Files"}).click();
    await page.locator('[data-x-path="0"]').selectOption("images/first.png");
    await page.locator('[data-x-action="add-path"][data-post="0"]').click();
    await page.getByRole("button", {name: "Choose from Files"}).click();
    await page.locator('[data-x-path="0"]').selectOption("images/second.png");
    await page.locator('[data-x-action="add-path"][data-post="0"]').click();
    await page.locator('[data-x-action="preview"][data-post="0"]').click();
    await page.locator("[data-x-exact]").waitFor();
    assert.match(await page.locator("[data-x-exact]").innerText(), /first\.png[\s\S]*second\.png/);
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator('[data-x-action="up"][data-media="1"]').first().click();
    assert.equal(await page.locator("[data-x-exact]").count(), 0);
    await page.locator('[data-x-action="preview"][data-post="0"]').click();
    assert.match(await page.locator("[data-x-exact]").innerText(), /second\.png[\s\S]*first\.png/);
    await page.locator('[data-x-action="publish"]').click();
    await page.getByText(/status may be uncertain/).waitFor();
    assert.equal(await page.evaluate(() => publishes), 1);
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => publishes), 1);
    await page.locator('[data-x-action="publish"]').click();
    assert.equal(await page.evaluate(() => publishes), 2);
    const keys = await page.evaluate(() => calls.filter(item => item.path.endsWith("/x/publish")).map(item => item.body.request_id));
    assert.equal(keys[0], keys[1]);
  } finally {await browser.close();}
});

test("late response from a closed project composer cannot replace the new project's draft", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route("http://localhost/x-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-post-test");
    await page.addScriptTag({path: "src/tin_lite/static/markdown-viewer.js"});
    await page.addScriptTag({path: "src/tin_lite/static/x-posts.js"});
    await page.evaluate(draft => {
      window.releaseOld = null;
      TinXPosts.open({projectId: "old", runId: "old-run", path: "old.json", api: async () => new Promise(resolve => {releaseOld = resolve;}), rawFetch: async () => new Response("")});
      const newer = structuredClone(draft); newer.posts[0].text = "New project post";
      TinXPosts.open({projectId: "new", runId: "new-run", path: "new.json", api: async () => ({revision: "b".repeat(40), draft: newer}), rawFetch: async () => new Response("")});
      releaseOld({revision: "a".repeat(40), draft});
    }, initial);
    await page.locator("[data-x-copy]").waitFor();
    assert.equal(await page.locator("[data-x-copy]").textContent(), "New project post");
    assert.equal(await page.locator(".x-posts-reader").count(), 1);
  } finally {await browser.close();}
});

test("missing assets need an explicit resolution and in-flight save locks editable fields", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.route("http://localhost/x-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/x-post-test");
    await page.addScriptTag({path: "src/tin_lite/static/markdown-viewer.js"});
    await page.addScriptTag({path: "src/tin_lite/static/x-posts.js"});
    await page.evaluate(draft => {
      window.saved = structuredClone(draft);
      window.releaseSave = null;
      TinXPosts.open({projectId: "project", runId: "run", path: "draft.json", api: async (path, options = {}) => {
        if (path.endsWith("/files")) return {files: [{path: "images/launch.png"}]};
        if (!options.method) return {revision: "a".repeat(40), draft: structuredClone(saved)};
        if (path.endsWith("/drafts")) return new Promise(resolve => {releaseSave = () => {saved = JSON.parse(options.body).draft; resolve({revision: "b".repeat(40), draft: structuredClone(saved)});};});
        return {preview_token: "token", text: saved.posts[1].text, account: {id: "123", username: "tin"}, attachments: []};
      }, rawFetch: async () => new Response("")});
    }, initial);
    await page.locator('[data-x-action="select"][data-post="1"]').click();
    const second = page.locator('.x-posts-card');
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByRole("button", {name: "Choose from Files"}).click();
    await second.locator('[data-x-path="1"]').selectOption("images/launch.png");
    await second.locator('[data-x-action="add-path"]').click();
    await page.getByRole("button", {name: "Draft notes", exact: true}).click();
    assert.match(await second.locator(".x-posts-notes").innerText(), /Screenshot/);
    await second.getByRole("button", {name: "Mark resolved"}).click();
    assert.equal(await second.getByRole("button", {name: "Mark resolved"}).count(), 0);
    await second.getByRole("button", {name: "Mark ready", exact: true}).click();
    await second.locator('[data-x-action="save"]').click();
    await page.waitForFunction(() => Boolean(window.releaseSave));
    assert.equal(await second.locator('[data-x-text="1"]').isDisabled(), true);
    assert.equal(await second.locator('[data-x-upload="1"]').isDisabled(), true);
    await page.evaluate(() => releaseSave());
    await second.locator('[data-x-text="1"]').waitFor({state: "visible"});
    await page.waitForFunction(() => !document.querySelector('[data-x-text="1"]').disabled);
    assert.deepEqual(await page.evaluate(() => ({readiness: saved.posts[1].readiness, missing_assets: saved.posts[1].missing_assets, attachments: saved.posts[1].attachments.map(item => item.path)})), {
      readiness: "ready", missing_assets: [], attachments: ["images/launch.png"],
    });
    await page.locator('[data-x-action="preview"]').click();
    await page.locator("[data-x-exact]").waitFor();
  } finally {await browser.close();}
});
