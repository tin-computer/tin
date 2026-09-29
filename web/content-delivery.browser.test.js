import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";

test("system approval keeps repository adaptation instead of direct Markdown publishing", async () => {
  const app = await fs.readFile("src/tin_lite/static/app.js", "utf8");
  const helpers = ["repositoryDeliveryAvailable", "isProposal", "decisionApprovalHtml"].map(name => app.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, "m"))[0]).join("\n");
  const render = new Function("isContentDraftReview", "connectedRepository", "escapeHtml", `${helpers}\nreturn decisionApprovalHtml;`)(() => true, () => "owner/site", text => text);
  assert.match(render({id: "draft"}, {}), /Publish now/);
  const system = render({id: "draft"}, {content_delivery: {system_run_id: "parent", approval_label: "Approve & open PR"}});
  assert.match(system, /Approve & open PR/);
  assert.doesNotMatch(system, /Publish now|github_commit/);
});

for (const theme of ["light", "dark"]) test(`content delivery: ${theme}, scoped settings and exact draft actions`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 1050}});
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("http://localhost/delivery-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/delivery-test");
    await page.setContent(`<html data-theme="${theme}"><body><main class="workspace"><article class="system-workflow-card content-program-card"><div class="system-config-body"><section class="content-program-work"><code class="system-config-kicker">what it works on</code><form id="plan"><button type="submit">Save upcoming work</button></form><div id="panel" data-content-delivery-program="program"></div></section><section class="system-config-when"><code class="system-config-kicker">when</code><p>Manual</p></section></div></article></main></body></html>`);
    await page.addStyleTag({path: "src/tin_lite/static/app.css"});
    const app = await fs.readFile("src/tin_lite/static/app.js", "utf8");
    const controls = app.slice(app.indexOf("function tinCounterControl("), app.indexOf("function bindWorkflowFieldValidation("));
    await page.addScriptTag({content: `${app.match(/^function escapeHtml\([\s\S]*?^}/m)[0]}\n${controls}`});
    await page.addScriptTag({path: "src/tin_lite/static/content-delivery.js"});
    await page.evaluate(async () => {
      window.writes = []; window.opened = []; window.toasts = []; window.planSubmits = 0;
      window.saved = {mode: "draft_only", repository: "owner/site", path_pattern: "content/blog/{slug}.md", frontmatter: {}, item_paths: {}};
      window.failSave = false;
      window.draftsFixture = {
        first: {run_id: "draft-one", has_output: true, stage: "awaiting_review", delivery: null},
        second: {run_id: "draft-two", has_output: true, delivery: {status: "completed", repository: "owner/site", path: "content/blog/two.md", pull_request: {number: 42, url: "https://github.com/owner/site/pull/42"}}},
        third: {run_id: "draft-three", has_output: true, delivery: {status: "failed", repository: "owner/site", path: "docs/setup.md", error: "Connection unavailable"}},
      };
      window.planFixture = {batches: [{items: [
        {id: "first", title: "Receive an agent reply", action: "new_page"},
        {id: "second", title: "Messaging delivery", action: "new_page"},
        {id: "third", title: "Set up your webhook", action: "update_page"},
      ]}]};
      window.contextFixture = {projectId: "project", toast: t => toasts.push(t), openRun: id => opened.push(id), api: async (path, options) => {
        if (options?.method) {
          writes.push({path, ...options});
          if (failSave) throw new Error("Settings changed; reload the saved version.");
          if (options.method === "PUT") saved = JSON.parse(options.body).settings;
          return {revision: "b".repeat(40), id: "delivery-run", status: "pending"};
        }
        if (path.endsWith("/delivery")) return {revision: "a".repeat(40), settings: structuredClone(saved), available_repository: "owner/site"};
        return {drafts: draftsFixture};
      }};
      document.querySelector("#plan").onsubmit = e => {e.preventDefault(); planSubmits++;};
      await TinContentDelivery.mount(document.body, contextFixture, {plan: planFixture, drafts: draftsFixture});
    });
    await page.getByText("Article delivery · Drafts in Tin", {exact: true}).click();
    assert.equal(await page.locator("[data-github-settings]").isVisible(), false);
    await page.getByRole("button", {name: "After approval", exact: true}).click();
    await page.locator('[data-tin-select-value="github_pr"]').click();
    assert.equal(await page.locator("[data-github-settings]").isVisible(), true);
    await page.getByLabel("New article files", {exact: true}).fill("posts/{slug}.md");
    await page.getByText("Site frontmatter and existing-page files", {exact: true}).click();
    await page.getByLabel("Frontmatter for new files", {exact: true}).fill('{"title":"{title}","date":"{date}"}');
    await page.getByLabel("File for Set up your webhook", {exact: true}).fill("docs/webhooks.md");
    await page.getByRole("button", {name: "Save delivery", exact: true}).click();
    const result = await page.evaluate(() => ({writes, planSubmits, toasts}));
    assert.equal(result.planSubmits, 0);
    assert.equal(result.writes.length, 1);
    assert.match(result.writes[0].path, /\/content-programs\/program\/delivery$/);
    assert.equal(JSON.parse(result.writes[0].body).settings.item_paths.third, "docs/webhooks.md");
    assert.equal(await page.locator("form form").count(), 0);
    assert.equal(await page.getByRole("button", {name: "Save delivery", exact: true}).isDisabled(), true);
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.locator('[data-read-draft="draft-one"]').click();
    assert.deepEqual(await page.evaluate(() => opened), ["draft-one"]);
    assert.equal(await page.getByRole("link", {name: "Open PR #42 ↗"}).getAttribute("href"), "https://github.com/owner/site/pull/42");
    await page.getByRole("button", {name: "Retry delivery →"}).click();
    assert.match(await page.evaluate(() => writes.at(-1).path), /\/content-drafts\/draft-three\/delivery\/retry$/);
    if (!await page.locator("[data-delivery-disclosure]").evaluate(d => d.open)) await page.getByText("Article delivery · GitHub PR", {exact: true}).click();
    await page.getByLabel("New article files", {exact: true}).fill("future/{slug}.md");
    await page.evaluate(() => {failSave = true;});
    await page.getByRole("button", {name: "Save delivery", exact: true}).click();
    assert.match(await page.locator("[data-delivery-message]").innerText(), /unsaved settings are still here/);
    // Re-rendering the containing card cannot discard unsaved delivery inputs.
    await page.evaluate(async () => {
      document.querySelector("#panel").outerHTML = '<div id="panel" data-content-delivery-program="program"></div>';
      await TinContentDelivery.mount(document.body, contextFixture, {plan: planFixture, drafts: draftsFixture});
    });
    assert.equal(await page.getByLabel("New article files", {exact: true}).inputValue(), "future/{slug}.md");
    assert.equal(await page.locator("[data-prepare-article-pr]").count(), 0);
    await page.evaluate(() => {failSave = false; draftsFixture.first.status = "succeeded"; draftsFixture.first.stage = "drafted";});
    await page.evaluate(() => {draftsFixture.first.system_delivery = {mode: "github_pr", binding: {repository: "owner/site"}};});
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByRole("button", {name: "Refresh delivery status"}).click();
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    assert.equal(await page.locator("[data-prepare-article-pr]").count(), 0);
    assert.match(await page.locator("#panel").innerText(), /organic system is preparing the article PR/);
    await page.evaluate(() => {delete draftsFixture.first.system_delivery;});
    // Restore the closed disclosure expected by the manual-delivery regression below.
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByRole("button", {name: "Refresh delivery status"}).click();
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByRole("button", {name: "Prepare PR →", exact: true}).click();
    const prepared = await page.evaluate(() => writes.at(-1));
    assert.equal(prepared.path, "/api/workflows/00000000-0000-4000-8000-000000000036/runs");
    assert.deepEqual(JSON.parse(prepared.body), {project_id: "project", inputs: {source_run_id: "draft-one", expected_repository: "owner/site", direction: ""}});
    assert.equal(await page.locator("[data-prepare-article-pr]").count(), 0);
    assert.equal(await page.getByLabel("New article files", {exact: true}).inputValue(), "future/{slug}.md");
    await page.evaluate(() => {draftsFixture.first.repository_delivery = {run_id: "failed-adaptation", status: "failed", repository: "owner/site", has_checkpoint: false};});
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByRole("button", {name: "Refresh delivery status"}).click();
    await page.getByText("Drafts and pull requests · 3", {exact: true}).click();
    await page.getByRole("button", {name: "Try adaptation again →", exact: true}).click();
    const retried = await page.evaluate(() => writes.at(-1));
    assert.equal(JSON.parse(retried.body).inputs.retry_run_id, "failed-adaptation");
    assert.notEqual(retried.headers["Idempotency-Key"], prepared.headers["Idempotency-Key"]);
    assert.equal(await page.locator("[data-prepare-article-pr]").count(), 0);
    for (const width of [1280, 768, 390]) {
      await page.setViewportSize({width, height: 1100});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_DELIVERY_SCREENSHOTS) {
        await fs.mkdir(process.env.TIN_DELIVERY_SCREENSHOTS, {recursive: true});
        await page.screenshot({path: `${process.env.TIN_DELIVERY_SCREENSHOTS}/delivery-${theme}-${width}.png`, fullPage: true});
      }
    }
    await page.evaluate(() => {
      document.body.insertAdjacentHTML("beforeend", `<form id="picker">${TinContentDelivery.fields()}</form>`);
      TinContentDelivery.bindPickers(document.body, {projectId: "project", api: async () => ({articles: [{run_id: "approved-run-id", title: "An approved article"}], repository: "owner/site"})});
    });
    await page.getByRole("button", {name: "Approved article", exact: true}).click();
    await page.getByRole("option", {name: "An approved article", exact: true}).click();
    assert.equal(await page.locator('#picker [name="input:source_run_id"]').inputValue(), "approved-run-id");
    assert.equal(await page.locator('#picker [name="input:expected_repository"]').inputValue(), "owner/site");
    assert.equal((await page.locator("#picker").innerText()).includes("approved-run-id"), false);
    await page.evaluate(() => TinContentDelivery.prepare(document.querySelector("#picker")));
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});
