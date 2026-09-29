// Packaged dashboard with synthetic APIs. No live credentials or projects.
// A browser side panel narrows the window: the page keeps its width and nothing overflows.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { fileUrl, openApp, serveApp } from "./app-fixture.js";

const box = (page, selector) => page.locator(selector).first().evaluate(node => {
  const rect = node.getBoundingClientRect();
  return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, visible: getComputedStyle(node).display !== "none"};
});

test("the menu moves to the top below 1100px and Decisions stacks on a narrow page", {timeout: 120000}, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 1101, 1000, 700]) {
      const {page, context, errors} = await openApp(browser, base, {viewport: {width, height: 900}, url: "/decisions"});
      await page.locator(".decision-detail-card").waitFor();
      const rail = await box(page, ".rail");
      const list = await box(page, ".decision-list");
      const card = await box(page, ".decision-detail-card");
      // The side menu is 248px; the top menu spans the page. A classic scrollbar (Linux CI)
      // takes part of the window, so compare with the page's width and allow for it.
      const pageWidth = await page.evaluate(() => document.documentElement.clientWidth);
      const onTop = rail.width >= pageWidth - 24 && rail.top <= 1;
      assert.equal(onTop, width <= 1100, `top menu at ${width}px (menu ${rail.width}px, page ${pageWidth}px, top ${rail.top})`);
      assert.ok(card.width >= 440, `card is ${card.width}px at ${width}px`);
      assert.equal(list.bottom <= card.top, width <= 760, `stacked at ${width}px`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no page overflow at ${width}px`);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
});

test("documents fit the space they get and show the section map only when it fits", {timeout: 120000}, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [1440, 1280, 1000, 700]) {
      const {page, context, errors} = await openApp(browser, base, {viewport: {width, height: 900}, url: fileUrl("reports/answers/page-1.md")});
      await page.locator(".markdown-section-link").first().waitFor({state: "attached"});
      const main = await box(page, "#main");
      const reading = await box(page, ".markdown-document");
      const map = await box(page, ".markdown-section-map");
      assert.ok(reading.left >= main.left && reading.right <= main.right, `document inside the page at ${width}px`);
      assert.equal(map.visible, main.width > 1100, `section map at ${width}px`);
      if (map.visible) assert.ok(map.left >= main.left, `section map inside the page at ${width}px`);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
});
