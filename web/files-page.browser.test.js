// Packaged Files pages with synthetic APIs. No live credentials or projects.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { fileUrl, openApp, serveApp } from "./app-fixture.js";

const fileKinds = [
  ["reports/answers/page-1.md", ".markdown-return"],
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
