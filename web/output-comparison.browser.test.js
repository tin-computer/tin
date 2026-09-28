// Isolated browser integration: real packaged app/assets, synthetic HTTP and identity.
// No product credentials, network effects, or authenticated production browser are used.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const runId = "11111111-1111-4111-8111-111111111111";
const original = `# Why small teams ship faster with one workflow

Most founders we talk to run growth from a spreadsheet, a handful of tabs, and a Monday reminder. This article looks at what changes when the routine becomes a workflow that runs on its own.

## What we measured

The median team went from 3 to 12 pages a month.
Numbers are medians across all 41 projects.

The gain came from two places:

- fewer hand-offs between research, drafting, and publishing
- a fixed weekly slot that does not move when the founder is busy

Every project kept a human on the final publish step.

Read the full method at https://tin.computer/reports/method/2026-09-small-teams-one-workflow?utm_source=article&utm_medium=web

## What did not change

Editing time per page stayed flat. The workflow does not write faster; it removes waiting.
`;
const current = original.replace("12 pages", "9 pages").replace("Numbers are medians across all 41 projects.", "> Caveat: the 41 projects are self-selected; teams that kept publishing are over-represented.");
const saved = original.replace(/Most founders[^\n]+/, "Most founders run growth from a spreadsheet, a few tabs, and a Monday reminder. When that routine becomes a workflow that runs on its own, the week changes shape: research lands on Tuesday, a draft on Thursday, and the publish step waits for one person.")
  .replace("\nEvery project kept a human on the final publish step.\n", "\n- drafts that arrive with sources already attached\n");

test("packaged app: Decisions, readers, exact apply/reload, and responsive comparison", async () => {
  const theme = process.env.TIN_TEST_THEME === "dark" ? "dark" : "light";
  let resolution = null;
  let loseResponse = false;
  let retryRequest = null;
  const writes = [];
  const errors = [];
  let ordinaryReview = null;
  let interrupted = false;
  const reviewApprovals = [];
  let filePath = "reports/PUBLIC_ARTICLE.md";
  let mediaType = "text/markdown";
  let currentBody = current;
  let savedBody = saved;
  const project = { id: "project", name: "Comparison QA", workspace_id: "workspace", workspace_name: "QA", timezone: "Europe/Berlin", member_count: 2 };
  const otherProject = { ...project, id: "other", name: "Other project" };
  const run = () => ({ id: runId, project_id: "project", workflow_name: interrupted ? "research.deep_dive" : ordinaryReview?.workflow_key || "content.public_article", workflow_title: interrupted ? "Deep research" : "Weekly article", status: ordinaryReview ? "needs_input" : "failed", created_at: "2026-09-07T11:20:00Z", output_resolution: resolution,
    artifact_path: ordinaryReview ? filePath : null, canonical_commit_sha: ordinaryReview ? "c".repeat(40) : null,
    error_message: interrupted ? "Codex stopped at this run's quoted spending maximum." : null,
    retained_output: ordinaryReview ? null : { reason: interrupted ? "execution_interrupted" : "output_conflict", artifact_path: interrupted ? "reports/RESEARCH.md" : filePath, revision: "c".repeat(40) } });
  const compare = () => ({ run_id: runId, project_id: "project", path: filePath, media_type: mediaType, complete: true, identical: false, resolution, allowed_actions: resolution ? [] : ["keep_current", "use_saved"],
    current: { presence: "file", content: currentBody, revision: "b".repeat(40) }, saved: { content: savedBody, revision: "c".repeat(40) }, starting: { content: original, available: true, presence: "file", revision: "a".repeat(40) } });
  const decisions = () => interrupted || resolution && resolution.state !== "applying" ? [] : [{ id: runId, run_id: runId, project_id: "project", kind: "output_conflict", workflow_key: "content.public_article", workflow_title: "Weekly article", title: "Choose a version of PUBLIC_ARTICLE.md", explanation: "This file changed while the workflow ran. Tin left the file alone and saved the result.", consequence: "Nothing changes until you confirm on the comparison.", created_at: run().created_at, items: [{ file: "reports/PUBLIC_ARTICLE.md", revision: "c".repeat(40), source: "retained" }] }];
  const document = { filename: "PUBLIC_ARTICLE.md", source_url: "", html: "<h1>Why small teams ship faster</h1><p>Current file.</p>", markdown: current, word_count: 120, reading_minutes: 1, headings: [] };
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = (value, type = "application/json") => { response.setHeader("Content-Type", type); response.end(type === "application/json" ? JSON.stringify(value) : value); };
    if (url.pathname.startsWith("/assets/")) {
      const file = path.resolve(assets, url.pathname.slice(8));
      if (!file.startsWith(`${assets}/`)) { response.writeHead(404).end(); return; }
      try { send(await fs.readFile(file), file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : file.endsWith(".svg") ? "image/svg+xml" : "application/octet-stream"); }
      catch { response.writeHead(404).end(); }
      return;
    }
    if (/^\/(?:system|chat|activity|decisions|files|file|document\/[^/]+|task\/[^/]+|compare\/[^/]+)?$/.test(url.pathname)) {
      let html = await fs.readFile(path.join(assets, "index.html"), "utf8");
      html = html.replace(/<script\b[^>]*src="\{\{CLERK[^>]+>[\s\S]*?<\/script>/g, "").replaceAll("{{ASSET_VERSION}}", "test");
      send(html, "text/html"); return;
    }
    if (url.pathname === "/api/projects") return send([project, otherProject]);
    if (url.pathname.startsWith("/api/projects/other/")) return send(url.pathname.endsWith("/system") ? { waiting_count: 0, running_count: 0, workflow_count: 0 } : []);
    if (url.pathname.endsWith("/decisions")) return send(ordinaryReview ? [ordinaryReview] : decisions());
    if (url.pathname === `/api/decisions/${runId}/apply` && request.method === "POST") {
      let body = ""; for await (const chunk of request) body += chunk;
      reviewApprovals.push(JSON.parse(body));
      return send({...run(), status: "running"});
    }
    if (url.pathname.endsWith("/system")) return send({ waiting_count: decisions().length, running_count: 0, workflow_count: 0 });
    if (url.pathname.endsWith("/runs")) return send([run()]);
    if (interrupted && url.pathname.endsWith("/activity")) return send([{id: "interrupted", run_id: runId, event_type: "workflow_failed", created_at: "2026-09-07T11:20:00Z", summary: "Research stopped at its spending maximum.", details: {}}]);
    if (url.pathname.endsWith(`/runs/${runId}`)) return send(run());
    if (url.pathname.endsWith(`/runs/${runId}/review`)) return send({
      run_id: runId, current_run_id: runId, version: 1, is_current: true,
      can_approve: true, can_request_changes: true, review_token: "review-version-test",
      artifact: {run_id: runId, assessment: false}, versions: [],
    });
    if (url.pathname.endsWith("/output-comparison")) return send(compare());
    if (url.pathname.endsWith("/output-resolution")) {
      if (request.method === "POST") {
        let body = ""; for await (const chunk of request) body += chunk;
        const choice = JSON.parse(body); writes.push(choice);
        if (loseResponse) {
          loseResponse = false; retryRequest = choice;
          resolution = { ...choice, state: "applying", revision: null };
          response.statusCode = 503;
          return send({ detail: { code: "resolution_pending", message: "The outcome is unconfirmed." } });
        }
        resolution = { ...choice, state: choice.action === "use_saved" ? "applied" : "kept", revision: "d".repeat(40), changed: choice.action === "use_saved" };
        return send(resolution);
      }
      return send({ project_id: "project", path: compare().path, resolution, retry_request: resolution?.state === "applying" ? retryRequest : null });
    }
    if (url.pathname.endsWith("/document")) return send(interrupted ? {...document, filename: "RESEARCH.md", html: "<h1>Why small teams ship faster</h1><p>Early research notes. The source review is unfinished.</p>"} : document);
    if (url.pathname.endsWith("/artifact")) return send(savedBody, mediaType);
    if (url.pathname.endsWith("/files/raw")) return send(currentBody, mediaType);
    if (url.pathname.endsWith("/files")) return send({ revision: "e".repeat(40), entries: [] });
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, ...(process.env.TIN_TEST_BROWSER_CHANNEL ? { channel: process.env.TIN_TEST_BROWSER_CHANNEL } : {}) });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript((resolvedTheme) => {
      window.Clerk = { load: async () => {}, isSignedIn: true, user: { id: "member", firstName: "QA" }, session: { getToken: async () => "synthetic-test-only" } };
      window.localStorage.setItem("tin-lite:theme", resolvedTheme);
      const applyTheme = () => {
        if (!document.documentElement) return false;
        document.documentElement.dataset.theme = resolvedTheme;
        return true;
      };
      if (!applyTheme()) {
        const observer = new MutationObserver(() => {
          if (applyTheme()) observer.disconnect();
        });
        observer.observe(document, { childList: true });
      }
    }, theme);
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    // Interrupted drafts remain readable, with no approval or apply action.
    interrupted = true;
    await page.goto(`${base}/#activity`);
    await page.getByRole("button", {name: "Partial result →", exact: true}).click();
    await page.getByRole("heading", {name: "Why small teams ship faster", exact: true}).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("source"), "retained");
    assert.match(await page.locator(".markdown-filename").textContent(), /^Partial result ·/);
    assert.equal(await page.getByRole("button", {name: /Approve|Use saved|Apply/}).count(), 0);
    if (process.env.TIN_RECOVERY_SCREENSHOT) await page.screenshot({path: process.env.TIN_RECOVERY_SCREENSHOT, clip: {x: 0, y: 0, width: 1440, height: 420}});
    assert.deepEqual(writes, []);
    interrupted = false;
    // Ordinary document reviews have one reader action per output, not a
    // second header shortcut to the first document. No-output reviews retain
    // their only route into the run; conflicts retain their distinct route.
    for (const workflow of ["content.diagram", "content.generate"]) {
      ordinaryReview = {...decisions()[0], kind: "review", workflow_key: workflow,
        workflow_title: workflow === "content.diagram" ? "Create a diagram" : "Draft planned content",
        explanation: "The output is ready for your review.", consequence: "",
        items: [{file: filePath, revision: "c".repeat(40)}]};
      await page.goto(`${base}/#decisions`);
      await page.locator(".decision-detail-card").waitFor();
      assert.equal(await page.locator("[data-decision-read]").count(), 0);
      assert.equal(await page.getByRole("button", {name: "Read →", exact: true}).count(), 1);
      assert.equal(await page.getByRole("button", {name: "Approve", exact: true}).count(), 1);
      await page.getByRole("button", {name: "Not now", exact: true}).click();
      assert.equal(reviewApprovals.length, 0);
      for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: 1000});
        assert.equal(await page.locator("[data-decision-read]").count(), 0);
        assert.equal(await page.getByRole("button", {name: "Read →", exact: true}).isVisible(), true);
        if (process.env.TIN_COMPARISON_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_COMPARISON_SCREENSHOTS}/decision-${workflow}-${theme}-${width}.png`, fullPage: true});
      }
      await page.getByRole("button", {name: "Read →", exact: true}).click();
      await page.getByRole("heading", {name: "Why small teams ship faster", exact: true}).waitFor();
      assert.equal(new URL(page.url()).pathname, `/document/${runId}`);
      assert.equal(reviewApprovals.length, 0);
    }
    ordinaryReview.items.push({file: "reports/SECOND.md", revision: "c".repeat(40)});
    await page.goto(`${base}/#decisions`);
    await page.locator(".decision-detail-card").waitFor();
    assert.equal(await page.locator("[data-decision-read]").count(), 0);
    assert.equal(await page.getByRole("button", {name: "Read →", exact: true}).count(), 2);
    await page.getByRole("button", {name: "Read →", exact: true}).nth(1).click();
    await page.waitForURL(/\/file\?/);
    const selectedFile = new URL(page.url()).searchParams;
    assert.equal(selectedFile.get("path"), "reports/SECOND.md");
    assert.equal(selectedFile.get("revision"), "c".repeat(40));
    assert.equal(selectedFile.get("reviewRun"), runId);
    await page.goto(`${base}/#decisions`);
    await page.locator(".decision-detail-card").waitFor();
    await page.getByRole("button", {name: "Approve", exact: true}).click();
    await page.getByText("Decision applied.", {exact: true}).waitFor();
    assert.equal(reviewApprovals.length, 1);
    assert.equal(reviewApprovals[0].action, "approve");
    ordinaryReview.items = [];
    await page.reload();
    await page.locator(".decision-detail-card").waitFor();
    assert.equal(await page.getByRole("button", {name: "Open run →", exact: true}).count(), 1);
    assert.equal(await page.getByRole("button", {name: "Read →", exact: true}).count(), 0);
    ordinaryReview = null;
    await page.setViewportSize({width: 1440, height: 1000});
    // A plain dashboard visit starts in My system; explicit routes still win.
    for (const hash of ["", "#/", "#unknown-view"]) {
      await page.goto(`${base}/${hash}`);
      await page.getByRole("heading", { name: "System", exact: true }).waitFor({ timeout: 5000 });
      assert.deepEqual(await page.locator(".nav-list .nav-item").evaluateAll(items => items.slice(0, 2).map(item => item.dataset.view)), ["workflows", "chat"]);
      assert.equal(await page.locator(".nav-list .nav-item.is-active").getAttribute("data-view"), "workflows");
      assert.equal(await page.locator(".workflow-section.is-active").getAttribute("data-workflow-section"), "yours");
      await page.reload();
      await page.getByRole("heading", { name: "System", exact: true }).waitFor({ timeout: 5000 });
    }
    await page.locator('.nav-item[data-view="chat"]').click();
    await page.locator("#chat-form").waitFor();
    assert.equal(new URL(page.url()).pathname, "/chat");
    await page.reload();
    await page.locator("#chat-form").waitFor();
    assert.equal(await page.locator(".nav-list .nav-item.is-active").getAttribute("data-view"), "chat");
    // External links and reloads initialize the first project; neither is a
    // project switch. Keep the exact reader route, source and revision intact.
    for (const hash of [
      `document/${runId}?return=decisions`,
      `document/${runId}?return=decisions&source=retained`,
      `document/${runId}?return=task&taskPath=reports%2FPUBLIC_ARTICLE.md`,
      `file?path=reports%2FPUBLIC_ARTICLE.md&revision=${"b".repeat(40)}&back=files`,
    ]) {
      await page.goto(`${base}/#${hash}`);
      await page.getByRole("heading", { name: "Why small teams ship faster", exact: true }).waitFor({ timeout: 5000 });
      assert.equal(new URL(page.url()).hash, "");
      const cleanUrl = page.url();
      assert.equal(new URL(cleanUrl).pathname, `/${hash.split("?")[0]}`);
      for (const [key, value] of new URLSearchParams(hash.split("?")[1])) assert.equal(new URL(cleanUrl).searchParams.get(key), value);
      await page.reload();
      await page.getByRole("heading", { name: "Why small teams ship faster", exact: true }).waitFor({ timeout: 5000 });
      assert.equal(page.url(), cleanUrl);
      assert.equal(writes.length, 0);
    }
    // An explicit switch must still leave the previous project's detail page.
    await page.goto(`${base}/?project=project#document/${runId}?return=decisions`);
    await page.getByRole("heading", { name: "Why small teams ship faster", exact: true }).waitFor();
    await page.locator("#project-switcher").click();
    await page.locator('[data-project-id="other"]').click();
    await page.getByRole("heading", { name: "System", exact: true }).waitFor();
    assert.equal(new URL(page.url()).pathname, "/system");
    assert.equal(new URL(page.url()).searchParams.get("project"), "other");
    assert.equal(await page.getByRole("heading", { name: "Why small teams ship faster", exact: true }).count(), 0);
    await page.locator("#project-switcher").click();
    await page.locator('[data-project-id="project"]').click();
    await page.getByRole("heading", { name: "System", exact: true }).waitFor();
    await page.goto(`${base}/#decisions`);
    await page.locator(".decision-detail-card").waitFor();
    await page.getByRole("button", {name: "Open run →", exact: true}).click();
    await page.getByRole("heading", {name: "Activity", exact: true}).waitFor();
    assert.equal(new URL(page.url()).pathname, "/activity");
    await page.goto(`${base}/#decisions`);
    await page.locator(".decision-detail-card").waitFor();
    if (theme === "dark") {
      assert.equal(await page.locator(".decision-detail-card").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.08)");
      assert.equal(await page.locator(".decision-output").first().evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.08)");
      assert.equal(await page.locator("#project-create-name").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.14)");
      assert.equal(await page.locator("#project-switcher").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(0, 0, 0, 0)");
    }
    await page.getByRole("button", { name: "Read →", exact: true }).click();
    await page.getByRole("button", { name: "← decisions", exact: true }).click();
    await page.getByRole("button", { name: "Compare", exact: true }).click();
    await page.locator(".compare-diff").waitFor();
    if (theme === "dark") {
      assert.equal(await page.locator("body").evaluate((node) => getComputedStyle(node).backgroundColor), "rgb(20, 18, 16)");
      assert.equal(await page.locator(".compare-card").evaluate((node) => getComputedStyle(node).backgroundColor), "rgb(27, 25, 23)");
      assert.equal(await page.locator(".compare-card").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.08)");
      assert.equal(await page.getByRole("button", { name: "Confirm choice", exact: true }).evaluate((node) => getComputedStyle(node).backgroundColor), "rgb(219, 68, 21)");
      assert.equal(await page.locator("html").evaluate((node) => getComputedStyle(node).colorScheme), "dark");
      assert.equal(await page.locator("#agent-rail").evaluate((node) => getComputedStyle(node).backgroundColor), "rgb(27, 25, 23)");
      assert.equal(await page.locator("#agent-rail").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.08)");
      await page.locator("#agent-rail-toggle").click();
      assert.equal(await page.locator("#agent-rail").evaluate((node) => getComputedStyle(node).backgroundColor), "rgb(27, 25, 23)");
      assert.equal(await page.locator("#agent-rail").evaluate((node) => getComputedStyle(node).borderTopColor), "rgba(246, 241, 231, 0.08)");
      if (process.env.TIN_COMPARISON_SCREENSHOTS) await page.screenshot({ path: `${process.env.TIN_COMPARISON_SCREENSHOTS}/agent-rail-open-1440.png`, fullPage: true });
      await page.locator("#agent-rail-toggle").click();
      const lightAppearance = page.getByRole("button", { name: "Use light appearance", exact: true }).first();
      const darkAppearance = page.getByRole("button", { name: "Use dark appearance", exact: true }).first();
      assert.equal(await darkAppearance.getAttribute("aria-pressed"), "true");
      await lightAppearance.click();
      assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
      assert.equal(await page.evaluate(() => window.localStorage.getItem("tin-lite:theme")), "light");
      await darkAppearance.click();
      assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
      assert.equal(await darkAppearance.getAttribute("aria-pressed"), "true");
    }
    assert.equal(await page.locator(".compare-line.is-later").count(), 4);
    assert.equal(await page.getByRole("radio", { name: "Keep current", exact: true }).isChecked(), true);
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Horizontal page overflow at ${width}`);
      assert(await page.getByRole("button", { name: "Confirm choice", exact: true }).isVisible());
      if (process.env.TIN_COMPARISON_SCREENSHOTS) await page.screenshot({ path: `${process.env.TIN_COMPARISON_SCREENSHOTS}/compare-${width}.png`, fullPage: true });
      if (width === 390) {
        await page.locator("#project-switcher").click();
        assert(await page.locator("#project-menu .project-menu-item").first().isVisible());
        assert.equal(await page.locator("#project-menu .appearance-control").count(), 0);
        assert.equal(await page.locator(".rail-footer .rail-sign-out").count(), 1);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        if (process.env.TIN_COMPARISON_SCREENSHOTS) await page.screenshot({ path: `${process.env.TIN_COMPARISON_SCREENSHOTS}/project-menu-390.png`, fullPage: true });
        await page.locator("#project-switcher").click();
      }
    }
    await page.getByRole("button", { name: "Read current file", exact: true }).click();
    await page.getByRole("button", { name: "← comparison", exact: true }).waitFor();
    assert.match(await page.locator("#main").textContent(), /current file at comparison/);
    await page.getByRole("button", { name: "← comparison", exact: true }).click();
    await page.locator(".compare-diff").waitFor();
    await page.getByRole("radio", { name: "Use saved result", exact: true }).check();
    await page.getByRole("button", { name: "Confirm choice", exact: true }).click();
    await page.getByRole("heading", { name: "File version decided" }).waitFor();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].expected_revision, "b".repeat(40));
    await page.reload();
    assert.equal(await page.locator("html").getAttribute("data-theme"), theme);
    await page.getByRole("heading", { name: "File version decided" }).waitFor();
    assert.equal(await page.locator(".compare-diff").count(), 0);
    assert.equal(await page.locator("#decision-count").textContent(), "");
    await page.getByRole("button", { name: "Read saved result →", exact: true }).click();
    await page.getByRole("button", { name: "← comparison", exact: true }).click();
    await page.getByRole("heading", { name: "File version decided" }).waitFor();
    resolution = null;
    loseResponse = true;
    await page.reload();
    await page.locator(".compare-diff").waitFor();
    await page.getByRole("button", { name: "Confirm choice", exact: true }).click();
    await page.getByRole("button", { name: "Check outcome", exact: true }).waitFor();
    const pending = structuredClone(writes.at(-1));
    await page.reload();
    await page.getByRole("button", { name: "Check outcome", exact: true }).click();
    await page.getByRole("heading", { name: "File version decided" }).waitFor();
    assert.deepEqual(writes.at(-1), pending);
    for (const format of ["csv", "mmd"]) {
      resolution = null;
      filePath = `reports/comparison.${format}`;
      mediaType = format === "csv" ? "text/csv" : "text/vnd.mermaid";
      currentBody = format === "csv" ? `name,note\nAna,${"a".repeat(200)}\n` : 'flowchart LR\n  draft["Draft"] --> file["File"]\n';
      savedBody = currentBody.replace(format === "csv" ? "Ana" : "Draft", format === "csv" ? "Bo" : "Review");
      await page.reload();
      await page.locator(".compare-diff.is-numbered").waitFor();
      assert(await page.locator(".compare-line-number").count() > 0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.getByRole("button", { name: "Read saved result", exact: true }).click();
      await page.getByRole("button", { name: "← comparison", exact: true }).waitFor();
      assert.match(await page.locator("#main").textContent(), /saved result/);
      assert.equal(await page.getByRole("button", { name: /Approve/ }).count(), 0);
      if (format === "csv") assert.equal(await page.locator("table").count(), 1);
      else await page.getByRole("button", { name: "Source", exact: true }).click();
      await page.getByRole("button", { name: "← comparison", exact: true }).click();
      await page.locator(".compare-diff").waitFor();
    }
    assert.deepEqual(errors, []);

    const systemContext = await browser.newContext({
      colorScheme: "dark",
      viewport: { width: 1440, height: 1000 },
    });
    await systemContext.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await systemContext.addInitScript(() => {
      window.Clerk = { load: async () => {}, isSignedIn: true, user: { id: "member", firstName: "QA" }, session: { getToken: async () => "synthetic-test-only" } };
    });
    const systemPage = await systemContext.newPage();
    await systemPage.goto(`${base}/#decisions`);
    assert.equal(await systemPage.locator("html").getAttribute("data-theme-preference"), "system");
    assert.equal(await systemPage.locator("html").getAttribute("data-theme"), "dark");
    assert.equal(
      await systemPage.getByRole("button", { name: "Use system appearance", exact: true }).first().getAttribute("aria-pressed"),
      "true",
    );
    await systemContext.close();
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
