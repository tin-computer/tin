// Synthetic Markdown output uses the same reader as articles and social batches.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import test from "node:test";
import {chromium} from "playwright";
import {fileUrl, openApp, serveApp} from "./app-fixture.js";

const path = "social/linkedin/drafts/2026-10-08-example.md";
const markdown = `# LinkedIn drafts

For: Ada

Choose the posts you want to use and tell Tin their numbers. You can ask for changes first. Nothing has been posted or scheduled.

## Post 1

The hardest part of handing off work is often explaining what happened before it.

We moved our project notes next to the work itself. Decisions, source material and drafts now have a shared home.

A small change, but it means the next person can start with the context instead of asking us to reconstruct it.

## Post 2

A shared folder is useful. A shared explanation is better.

When we save a draft, we also save the notes behind it. The next person can see what we were trying to say and where the facts came from.

That makes feedback more specific: change the argument, add a missing example, or question the source.

## Post 3

Before handing something off, I ask: could someone understand this without asking me to retell the conversation?

If the answer is no, the handoff still depends on me.

Keeping the decision beside the work has helped us see where that context was missing.
`;
const document = JSON.parse(execFileSync("uv", ["run", "python", "-c", `import json, sys
from tin_lite.documents import render_markdown
r = render_markdown(sys.stdin.read())
print(json.dumps(dict(html=r.html, word_count=r.word_count, reading_minutes=r.reading_minutes)))`], {input: markdown, encoding: "utf8"}));

test("LinkedIn alternatives open as an ordinary document with section navigation", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    for (const theme of ["light", "dark"]) {
      const {page, context, errors} = await openApp(browser, base, {theme});
      const mutations = [];
      page.on("request", request => {if (request.method() !== "GET") mutations.push(request.url());});
      await page.route("**/files/document?*", route => route.fulfill({json: {
        filename: path.split("/").at(-1), ...document, related_documents: [],
      }}));
      await page.goto(`${base}${fileUrl(path)}&project=project`);
      await page.getByRole("heading", {name: "LinkedIn drafts", exact: true}).waitFor();
      assert.equal(await page.locator(".markdown-document h2").count(), 3);
      assert.equal(await page.locator(".markdown-section-link").count(), 3);
      assert.equal(await page.getByRole("button", {name: /Save draft|Copy post|Publish|Approve draft|Request changes/}).count(), 0);
      assert.match(await page.locator(".markdown-document").textContent(), /tell Tin their numbers/);
      await page.locator(".markdown-section-link").filter({hasText: "Post 2"}).click();
      assert.equal(await page.locator(".markdown-document h2").nth(1).isVisible(), true);
      for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: 1000});
        await page.evaluate(() => window.scrollTo(0, 0));
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        const rect = await page.locator(".markdown-document").boundingBox();
        assert.ok(rect.x >= 0 && rect.x + rect.width <= width);
        if (process.env.TIN_LINKEDIN_DRAFT_SCREENSHOTS) await page.screenshot({
          path: `${process.env.TIN_LINKEDIN_DRAFT_SCREENSHOTS}-${theme}-${width}.png`, fullPage: true,
        });
      }
      assert.deepEqual(mutations, []);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
