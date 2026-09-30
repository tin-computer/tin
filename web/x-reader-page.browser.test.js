import assert from "node:assert/strict";
import test from "node:test";
import {chromium} from "playwright";
import {serveApp, openApp, revision} from "./app-fixture.js";

const draftPath = "social/x-drafts/2026-09-29-project-context.json";
const initial = {schema_version: "tin.social.x_draft.v1", account_id: "123", posts: [
  {id: "p1", text: "The useful part of a coding agent isn’t just the code it writes. It’s the context it can reuse.\n\nTin carries that context into marketing workflows—so a draft can start from project files instead of another blank brief.", readiness: "ready", attachments: [], missing_assets: [], editor_notes: "Lead with the working principle. Save the demo for a separate post.", support: [{source_path: "wiki/INDEX.md", excerpt: "Workflows read current project files."}]},
  {id: "p2", text: "One project, one set of files. Your coding agent and your marketing workflows should be able to work from the same context.", readiness: "needs_asset", attachments: [], missing_assets: ["A short demo of the Files view"], editor_notes: "Attach a real demo before publishing.", support: []},
]};

async function setup(browser, base, viewport) {
  const opened = await openApp(browser, base, {viewport, url: "/files"});
  let saved = structuredClone(initial);
  const calls = [];
  await opened.page.route(url => new URL(url).pathname.startsWith("/api/projects/project/x/"), async route => {
    const request = route.request(), path = new URL(request.url()).pathname, body = request.postDataJSON();
    calls.push({path, method: request.method(), body});
    if (path.endsWith("/drafts")) {
      if (request.method() === "PUT") saved = structuredClone(body.draft);
      return route.fulfill({json: {path: draftPath, revision, draft: saved}});
    }
    if (path.endsWith("/preview")) return route.fulfill({json: {preview_token: "token", account: {id: "123", username: "@example"}, text: saved.posts.find(p => p.id === body.post_id).text, attachments: []}});
    throw new Error(`Unexpected write: ${path}`);
  });
  const url = `${base}/files?${new URLSearchParams({project: "project", x_draft: draftPath})}`;
  await opened.page.goto(url);
  await opened.page.locator("[data-x-copy]").waitFor();
  return {...opened, calls, url};
}

test("X reader follows dashboard navigation and keeps unsaved edits without keeping a publish preview", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors, calls, url} = await setup(browser, base);
    assert.equal(await page.locator("dialog[open]").count(), 0);
    assert.equal(await page.locator('.nav-item.is-active').getAttribute("data-view"), "files");
    assert.equal(await page.locator("[data-x-text]").count(), 0);
    assert.equal(await page.locator(".x-posts-reader .x-posts-card").count(), 1);
    await page.getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator("[data-x-text]").fill("An edit kept while I check the other post.");
    await page.locator('[data-x-action="select"][data-post="1"]').click();
    assert.match(await page.locator("[data-x-copy]").textContent(), /One project/);
    await page.locator('[data-x-action="select"][data-post="0"]').click();
    assert.equal(await page.locator("[data-x-copy]").textContent(), "An edit kept while I check the other post.");
    await page.locator("[data-x-close]").click();
    await page.locator("#files-search").waitFor();
    await page.goBack();
    await page.locator("[data-x-copy]").waitFor();
    assert.equal(await page.locator("[data-x-copy]").textContent(), "An edit kept while I check the other post.");
    await page.getByRole("button", {name: "Preview post", exact: true}).click();
    await page.locator("[data-x-exact]").waitFor();
    assert.equal(calls.filter(c => c.path.endsWith("/drafts") && c.method === "PUT").length, 1);
    assert.equal(calls.filter(c => c.path.endsWith("/publish")).length, 0);
    await page.locator("[data-x-close]").click();
    await page.goBack();
    await page.locator("[data-x-copy]").waitFor();
    assert.equal(await page.locator('[data-x-action="publish"]').count(), 0);
    await page.reload();
    await page.locator("[data-x-copy]").waitFor();
    assert.equal(page.url(), url);
    assert.equal(await page.locator("[data-x-copy]").textContent(), "An edit kept while I check the other post.");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});

test("Paper-based X reader and editor stay within the screen and use the active theme", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await setup(browser, base, {width: 1440, height: 1000});
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => TinTheme.apply(value), theme);
      for (const width of [1440, 768, 390]) {
        await page.setViewportSize({width, height: 1000});
        await page.evaluate(() => document.fonts.ready);
        const layout = await page.evaluate(() => {
          const reader = document.querySelector(".x-posts-reader");
          const styles = getComputedStyle(reader);
          return {overflow: document.documentElement.scrollWidth > innerWidth,
            background: styles.backgroundColor, expected: getComputedStyle(document.body).backgroundColor,
            color: styles.color, width: document.querySelector(".x-posts-column").getBoundingClientRect().width};
        });
        assert.equal(layout.overflow, false, `${theme} at ${width}`);
        assert.equal(layout.background, layout.expected);
        assert.notEqual(layout.color, layout.background);
        assert.ok(layout.width <= 680);
        if (process.env.TIN_X_SCREENSHOTS) await page.screenshot({path: `/tmp/tin-x-reader-${theme}-${width}.png`, fullPage: true, animations: "disabled"});
        await page.getByRole("button", {name: "Edit post", exact: true}).click();
        await page.getByRole("button", {name: "Choose from Files", exact: true}).click();
        await page.locator("[data-x-path]").waitFor();
        assert.equal(await page.locator('[data-x-path] option[value="brand/logo.png"]').count(), 1);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        if (process.env.TIN_X_SCREENSHOTS) await page.screenshot({path: `/tmp/tin-x-editor-${theme}-${width}.png`, fullPage: true, animations: "disabled"});
        await page.getByRole("button", {name: "Close editing", exact: true}).click();
      }
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.getByRole("button", {name: "Preview post", exact: true}).click();
    await page.locator("[data-x-exact]").waitFor();
    if (process.env.TIN_X_SCREENSHOTS) await page.screenshot({path: "/tmp/tin-x-preview-dark.png", fullPage: true, animations: "disabled"});
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});
