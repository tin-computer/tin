// Packaged Files pages with synthetic APIs. No live credentials or projects.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { fileUrl, openApp, serveApp } from "./app-fixture.js";

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
