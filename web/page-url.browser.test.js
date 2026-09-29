import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";

const url = "https://example.com/learn/which-tools";
const pages = {
  proposed: {url, state: "proposed", checkable: true, note: "Approving commits content/learn/which-tools.md to owner/site."},
  planned: {url, state: "planned", checkable: false},
  noRoute: {url, state: "proposed", route_missing: true, checkable: false, note: "Tin found no page on your site that shows files from content/answers/."},
  prOpen: {url, state: "planned", pull_request: {url: "https://github.com/owner/site/pull/34", number: 34}, checkable: true},
  merged: {url, state: "merged", pull_request: {url: "https://github.com/owner/site/pull/34", number: 34}, deploy_overdue: false, checkable: true},
  committedLate: {url, state: "merged", pull_request: null, deploy_overdue: true, checkable: true},
  live: {url, state: "live", checkable: false},
};

async function withPage(run) {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 800}});
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("http://localhost/page-url-test", (route) => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/page-url-test");
    await page.addStyleTag({path: "src/tin_lite/static/app.css"});
    await page.addScriptTag({path: "src/tin_lite/static/page-url.js"});
    await run(page);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
}

test("the call sites put one line under the card's sentence and at the top of the document column", async () => {
  const app = await fs.readFile("src/tin_lite/static/app.js", "utf8");
  assert.match(app, /<p class="decision-summary">\$\{escapeHtml\(bodyLine\)\}<\/p>` : ""\}\n\s*\$\{decision\.kind !== "output_conflict" \? pageUrlLine\(run\) : ""\}/);
  assert.match(app, /querySelector\?\.\("\.markdown-document"\)/);
  assert.match(app, /readerColumn\.insertAdjacentHTML\("afterbegin", pageUrlLine\(run, "document"\)\)/);
  const index = await fs.readFile("src/tin_lite/static/index.html", "utf8");
  assert.ok(index.indexOf("/assets/page-url.js") < index.indexOf("/assets/app.js"));
});

test("before approval the card shows only where the page would appear, and nothing without a route", async () => {
  await withPage(async (page) => {
    const result = await page.evaluate((pages) => {
      document.body.innerHTML = '<article class="decision-detail-card"><div class="decision-detail-body"><p>An answer page is ready for your review.</p><div id="card"></div></div></article>';
      const card = document.querySelector("#card");
      const draw = (value) => {
        card.innerHTML = TinPageUrl.card(value, "run-1");
        const line = card.querySelector("p");
        return line ? {text: line.innerText.replace(/\s+/g, " ").trim(), links: line.querySelectorAll("a").length, label: getComputedStyle(line.querySelector(".page-url-label")).fontFamily, address: getComputedStyle(line.querySelector("code")).fontFamily} : null;
      };
      return {
        proposed: draw(pages.proposed),
        planned: draw(pages.planned),
        noRoute: card.innerHTML = TinPageUrl.card(pages.noRoute, "run-1"),
        afterApproval: [pages.prOpen, pages.merged, pages.live].map((value) => TinPageUrl.card(value, "run-1")),
        pending: TinPageUrl.card(null, "run-2", {pending: true}),
      };
    }, pages);
    assert.equal(result.proposed.text, `Proposed URL ${url}`);
    assert.equal(result.proposed.links, 0, "a proposed address is text, never a link");
    assert.doesNotMatch(result.proposed.text, /Approving|Markdown/);
    assert.match(result.proposed.address, /mono|Menlo|Geist Mono/i);
    assert.doesNotMatch(result.proposed.label, /mono/i);
    assert.equal(result.planned.text, `Will be published at ${url}`);
    assert.equal(result.noRoute, "");
    assert.deepEqual(result.afterApproval, ["", "", ""]);
    assert.match(result.pending, /data-page-url-due hidden/);
  });
});

test("after approval the document says what happened, and links the page only once it is live", async () => {
  await withPage(async (page) => {
    const result = await page.evaluate((pages) => {
      document.body.innerHTML = '<section class="markdown-viewer is-in-app"><header class="markdown-context-bar">path</header><div class="markdown-viewer-body"><div class="markdown-reader-layout"><nav class="markdown-section-map"></nav><article class="markdown-document"><h1>Which tools work with coding agents?</h1></article></div></div></section>';
      const column = document.querySelector(".markdown-document");
      const draw = (value) => {
        column.querySelector(".page-url-status")?.remove();
        column.insertAdjacentHTML("afterbegin", TinPageUrl.document(value, "run-1"));
        const line = column.querySelector(".page-url-status");
        if (!line) return null;
        const box = line.getBoundingClientRect(), title = column.querySelector("h1").getBoundingClientRect(), bar = document.querySelector(".markdown-context-bar").getBoundingClientRect();
        return {text: line.innerText, href: line.querySelector("a")?.getAttribute("href") || null, first: column.firstElementChild === line, aligned: Math.abs(box.left - title.left) < 1, above: box.bottom <= title.top, gap: Math.round(box.top - bar.bottom)};
      };
      return {
        prOpen: draw(pages.prOpen),
        merged: draw(pages.merged),
        committedLate: draw(pages.committedLate),
        live: draw(pages.live),
        beforeApproval: [pages.proposed, pages.planned, pages.noRoute].map((value) => TinPageUrl.document(value, "run-1")),
      };
    }, pages);
    assert.equal(result.prOpen.text, "Pull request #34 is open; the page goes live after you merge it.");
    assert.equal(result.prOpen.href, null);
    assert.ok(result.prOpen.first && result.prOpen.aligned && result.prOpen.above, "under the path bar, above the title, in the document column");
    assert.ok(result.prOpen.gap >= 0 && result.prOpen.gap <= 32, `directly under the path bar (${result.prOpen.gap}px)`);
    assert.equal(result.merged.text, "Merged. Waiting for example.com to deploy it.");
    assert.equal(result.committedLate.text, "Committed, but not a page on example.com yet.");
    assert.equal(result.live.text, `Live at ${url} ↗`);
    assert.equal(result.live.href, url);
    assert.deepEqual(result.beforeApproval, ["", "", ""]);
  });
});

test("a due line asks the server once and redraws in its own mode", async () => {
  await withPage(async (page) => {
    const result = await page.evaluate(async (pages) => {
      document.body.innerHTML = `<div id="card">${TinPageUrl.card(pages.proposed, "run-1")}</div><article class="markdown-document">${TinPageUrl.document(pages.prOpen, "run-1")}</article>`;
      const asked = [], updates = [];
      const answers = {"card": pages.noRoute, "document": pages.live};
      let call = 0;
      const api = async (path) => {
        asked.push(path);
        return {page_url: call++ === 0 ? answers.card : answers.document};
      };
      TinPageUrl.bind(document.body, {api, onUpdate: (id, value) => updates.push([id, value.state])});
      TinPageUrl.bind(document.body, {api});
      await new Promise((resolve) => setTimeout(resolve, 50));
      const unsafe = TinPageUrl.document({...pages.live, url: "javascript:alert(1)"}, "run-4");
      return {asked, updates, card: document.querySelector("#card").innerHTML, document: document.querySelector(".page-url-status")?.innerText, unsafe};
    }, pages);
    assert.deepEqual(result.asked, [
      "/api/workflows/runs/run-1/page-url?check=true",
      "/api/workflows/runs/run-1/page-url?check=true",
    ]);
    assert.deepEqual(result.updates, [["run-1", "proposed"], ["run-1", "live"]]);
    assert.equal(result.card, "", "a route check that finds no page removes the card line");
    assert.equal(result.document, `Live at ${url} ↗`);
    assert.equal(result.unsafe, "");
  });
});
