import assert from "node:assert/strict";
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
        if (path.includes("/x/drafts")) return {feedback_run_id: "source", revision: "a".repeat(40), draft: {schema_version: "tin.social.x_draft.v1", account_id: "123", posts: [{...post, text: done ? "try the demo." : post.text}]}};
        if (path.includes("/review?")) return {run_id: "source", review_token: "b".repeat(64), can_request_changes: true,
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
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});
