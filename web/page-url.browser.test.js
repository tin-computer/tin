import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";

const proposed = {
  url: "https://example.com/answers/which-tools",
  state: "proposed",
  label: "Proposed URL",
  note: "Tin found no page on your site that shows files from content/answers/. Approving commits content/answers/which-tools.md to owner/site as a Markdown file only.",
  checkable: true,
};
const live = {
  url: "https://example.com/learn/which-tools",
  state: "live",
  label: "Live at",
  note: "Tin found the page on your site.",
  checked_at: new Date(Date.now() - 5 * 60000).toISOString(),
  checkable: false,
};

test("the decision card shows where a page would appear and links it only once it is live", async () => {
  const app = await fs.readFile("src/tin_lite/static/app.js", "utf8");
  assert.match(app, /\$\{decision\.kind !== "output_conflict" \? pageUrlLine\(run\) : ""\}/);
  assert.match(app, /readerBody\.insertAdjacentHTML\("afterbegin", pageUrlLine\(run\)\)/);
  const index = await fs.readFile("src/tin_lite/static/index.html", "utf8");
  assert.ok(index.indexOf("/assets/page-url.js") < index.indexOf("/assets/app.js"));

  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 700}});
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("http://localhost/page-url-test", (route) => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/page-url-test");
    await page.setContent('<html><body><main><article class="decision-detail-card"><div class="decision-detail-body" id="card"></div></article></main></body></html>');
    await page.addStyleTag({path: "src/tin_lite/static/app.css"});
    await page.addScriptTag({path: "src/tin_lite/static/page-url.js"});

    const result = await page.evaluate(async ({proposed, live}) => {
      const card = document.querySelector("#card");
      const asked = [];
      card.innerHTML = TinPageUrl.html(proposed, "run-1") + TinPageUrl.html(null, "run-2", {pending: true}) + TinPageUrl.html(null, "run-3");
      const before = {
        text: card.querySelector('[data-page-url="run-1"]').innerText,
        links: card.querySelectorAll('[data-page-url="run-1"] a').length,
        hidden: card.querySelector('[data-page-url="run-2"]').hidden,
        missing: !card.querySelector('[data-page-url="run-3"]'),
      };
      const updates = [];
      TinPageUrl.bind(card, {
        api: async (path) => {
          asked.push(path);
          return {page_url: path.includes("run-2") ? live : proposed};
        },
        onUpdate: (id, value) => updates.push([id, value.state]),
      });
      TinPageUrl.bind(card, {api: async (path) => {asked.push(path); return {}}});
      await new Promise((resolve) => setTimeout(resolve, 50));
      const liveLine = card.querySelector('[data-page-url="run-2"]');
      const unsafe = TinPageUrl.html({...live, url: "javascript:alert(1)"}, "run-4");
      return {before, asked, updates, liveText: liveLine.innerText, liveHref: liveLine.querySelector("a")?.href, liveTarget: liveLine.querySelector("a")?.rel, unsafe, hidden: liveLine.hidden};
    }, {proposed, live});

    assert.match(result.before.text, /Proposed URL/);
    assert.match(result.before.text, /https:\/\/example\.com\/answers\/which-tools/);
    assert.match(result.before.text, /as a Markdown file only/);
    assert.equal(result.before.links, 0, "a proposed URL is text, never a link");
    assert.equal(result.before.hidden, true);
    assert.equal(result.before.missing, true);
    // Each due line is asked for once, even when the card binds again.
    assert.deepEqual(result.asked, [
      "/api/workflows/runs/run-1/page-url?check=true",
      "/api/workflows/runs/run-2/page-url?check=true",
    ]);
    assert.deepEqual(result.updates, [["run-1", "proposed"], ["run-2", "live"]]);
    assert.match(result.liveText, /Live at/);
    assert.match(result.liveText, /Checked 5 min ago/);
    assert.equal(result.liveHref, "https://example.com/learn/which-tools");
    assert.equal(result.liveTarget, "noopener noreferrer");
    assert.equal(result.hidden, false);
    assert.equal(result.unsafe, "");
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
