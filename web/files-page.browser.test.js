// Packaged Files pages with synthetic APIs. No live credentials or projects.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { fileUrl, openApp, revision, serveApp } from "./app-fixture.js";
import fs from "node:fs/promises";

test("file modified dates appear in the tree, search and narrow folder list", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless:true});
  try {
    for (const theme of ["light", "dark"]) {
      const {page, context, errors} = await openApp(browser, base, {url:"/files", theme, viewport:{width:1440,height:900}});
      await page.route("**/api/projects/project/files?*", route => {
        assert.equal(new URL(route.request().url()).searchParams.get("include_modified"), "true");
        return route.fulfill({json:{revision, files:[
          {path:"drafts/product-update.md", modified_at:"2026-09-20T12:00:00Z"},
          {path:"drafts/older-draft.md", modified_at:"2025-08-10T12:00:00Z"},
          {path:"drafts/no-date.md"},
        ]}});
      });
      await page.reload();
      assert.equal(await page.locator("html").getAttribute("data-theme"), theme);
      const row = page.locator('[data-item-path="drafts/product-update.md"]');
      await row.waitFor();
      assert.match(await row.innerText(), /Sep 20, 2026/);
      assert.match(await row.locator('[title^="Last modified"]').getAttribute("title"), /2026/);
      assert.match(await page.locator('[data-item-path="drafts/no-date.md"]').innerText(), /—/);
      if (process.env.TIN_FILES_SCREENSHOTS) {
        await fs.mkdir(process.env.TIN_FILES_SCREENSHOTS, {recursive:true});
        await page.screenshot({path:`${process.env.TIN_FILES_SCREENSHOTS}/files-dates-${theme}.png`});
      }
      await page.locator("#files-search").fill("product-update");
      assert.match(await page.locator("#files-search-results").innerText(), /Sep 20, 2026/);
      await page.locator("#files-search").fill("");
      await page.setViewportSize({width:390,height:844});
      await page.locator('#files-mobile-drill [data-files-directory="drafts"]').click();
      assert.match(await page.locator('#files-mobile-drill [data-project-file="drafts/product-update.md"]').innerText(), /Sep 20, 2026/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_FILES_SCREENSHOTS) await page.screenshot({path:`${process.env.TIN_FILES_SCREENSHOTS}/files-dates-${theme}-390.png`});
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {await browser.close(); server.close();}
});

const fileKinds = [
  ["reports/answers/page-1.md", ".markdown-return"],
  ["brand/logo.png", "[data-back-files]"],
  ["notes.txt", "[data-back-files]"],
  ["reports/keyword-plan/keywords.csv", ".project-file-return"],
  ["reports/keyword-plan/keywords.json", ".project-file-return"],
  ["diagrams/flow.mmd", "[data-back-files]"],
];

test("every file page keeps the same back button at every width", {timeout: 120000}, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 1000, 700]) {
      const {page, context, errors} = await openApp(browser, base, {viewport: {width, height: 900}, url: "/files"});
      for (const [filePath, selector] of fileKinds) {
        await page.goto(`${base}${fileUrl(filePath)}&project=project`);
        const back = page.locator(selector).first();
        await back.waitFor({state: "visible"});
        assert.equal((await back.textContent()).trim(), "← files", `${filePath} at ${width}px`);
      }
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
});

test("each folder in an open file's path opens that folder in Files", {timeout: 120000}, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/files"});
    for (const [filePath] of fileKinds) {
      await page.goto(`${base}${fileUrl(filePath)}&project=project`);
      const trail = page.getByRole("navigation", {name: "File path"});
      await trail.waitFor();
      assert.equal(await trail.textContent(), filePath);
      const folders = filePath.split("/").slice(0, -1);
      assert.deepEqual(await trail.getByRole("button").allTextContents(), folders, filePath);
      assert.equal(await trail.locator('[aria-current="page"]').textContent(), filePath.split("/").at(-1));
    }

    // Desktop: the tree opens with that folder expanded and selected.
    await page.goto(`${base}${fileUrl("reports/keyword-plan/keywords.json")}&project=project`);
    await page.getByRole("navigation", {name: "File path"}).getByRole("button", {name: "keyword-plan"}).click();
    await page.locator(".files-view").waitFor();
    const folder = page.locator('[data-item-path="reports/keyword-plan/"]');
    await folder.waitFor();
    assert.equal(await folder.getAttribute("aria-expanded"), "true");
    assert.equal(await folder.getAttribute("aria-selected"), "true");
    await page.locator('[data-item-path="reports/keyword-plan/keywords.csv"]').waitFor();
    assert.deepEqual(errors, []);
    await context.close();

    // Narrow: the folder list opens at that folder.
    const narrow = await openApp(browser, base, {viewport: {width: 700, height: 900}, url: fileUrl("brand/proposals/2026-09-28-1a2b3c4d/BRAND.md")});
    await narrow.page.getByRole("navigation", {name: "File path"}).getByRole("button", {name: "proposals"}).click();
    const crumbs = narrow.page.locator("#files-mobile-drill .files-breadcrumb");
    await crumbs.waitFor();
    assert.equal(await crumbs.textContent(), "Project/brand/proposals");
    await narrow.page.locator('#files-mobile-drill [data-files-directory="brand/proposals/2026-09-28-1a2b3c4d"]').waitFor();
    assert.deepEqual(narrow.errors, []);
    await narrow.context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("tree rows keep the right icon while folders open and close", {timeout: 120000}, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/files"});
    await page.locator('[data-item-path="brand/"]').waitFor();
    const wrongIcons = () => page.evaluate(() => {
      const root = [...document.querySelectorAll("#project-file-tree *")].find(node => node.shadowRoot).shadowRoot;
      return [...root.querySelectorAll('[data-type="item"]')].flatMap(row => {
        const icons = [...row.querySelectorAll('[data-item-section="icon"] svg use')].map(use => use.getAttribute("href"));
        const expected = row.dataset.itemType === "folder" ? ["#tin-tree-folder"] : null;
        const ok = expected ? icons.join() === expected.join() : icons.length === 1 && icons[0] !== "#tin-tree-folder";
        return ok ? [] : [`${row.dataset.itemPath}: ${icons.join(" ") || "no icon"}`];
      });
    });
    // Rows are reused as folders close and open; a reused row must not keep a folder's icon.
    for (const folder of ["brand/", "reports/", "brand/", "context/", "reports/", "reports/answers/", "reports/keyword-plan/", "brand/"]) {
      await page.locator(`[data-item-path="${folder}"]`).click();
      await page.waitForTimeout(100);
      assert.deepEqual(await wrongIcons(), [], `after toggling ${folder}`);
    }
    const open = await page.locator('[data-item-path="reports/"]').evaluate(row => getComputedStyle(row).getPropertyValue("--tin-tree-open").trim());
    const closed = await page.locator('[data-item-path="brand/"]').evaluate(row => getComputedStyle(row).getPropertyValue("--tin-tree-open").trim());
    assert.deepEqual([open, closed], ["1", ""]);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("canonical X draft JSON in Files opens the editor without replacing the generic reader", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/files"});
    const chosen = [];
    await page.route(url => new URL(url).pathname === "/api/projects/project/x/drafts", route => {
      chosen.push(new URL(route.request().url()).searchParams.get("path"));
      return route.fulfill({json: {
        path: "social/x-drafts/release.json", revision,
        draft: {schema_version: "tin.social.x_draft.v1", account_id: "123", posts: [{
          id: "p1", text: "One saved X post", readiness: "ready", support: [],
          editor_notes: "", attachments: [], missing_assets: [],
        }]},
      }});
    });
    await page.goto(`${base}${fileUrl("social/x-drafts/release.json")}&project=project`);
    await page.locator(".project-file-view.is-json").waitFor();
    assert.equal(await page.locator(".project-json-body").isVisible(), true);
    await page.getByRole("button", {name: "Edit X drafts"}).click();
    await page.locator("[data-x-copy]").waitFor();
    assert.equal(await page.locator("[data-x-copy]").textContent(), "One saved X post");
    assert.equal(new URL(page.url()).searchParams.get("x_draft"), "social/x-drafts/release.json");
    assert.equal(await page.locator("dialog[open]").count(), 0);
    assert.deepEqual(chosen, ["social/x-drafts/release.json"]);
    await page.locator("[data-x-close]").click();
    await page.locator(".project-json-body").waitFor();
    assert.equal(await page.locator(".project-json-body").isVisible(), true);
    await page.goto(`${base}${fileUrl("reports/keyword-plan/keywords.json")}&project=project`);
    await page.locator(".project-file-view.is-json").waitFor();
    assert.equal(await page.getByRole("button", {name: "Edit X drafts"}).count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});
