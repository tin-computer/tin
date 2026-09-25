// Packaged dashboard, synthetic HTTP only. No supplier calls or production mutations.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
for (const hidden of [false, true]) for (const theme of ["light", "dark"]) test(`article feedback: ${theme}, ${hidden ? "hidden public article with GitHub" : "discovered content draft"}, clean reader, inline card, replay and comparison`, async () => {
  const project = {id: "project", name: "ClawMessenger", workspace_id: "workspace", workspace_name: "ClawMessenger", timezone: "Europe/Berlin", member_count: 1};
  const workflow = {id: "article", key: hidden ? "content.public_article" : "content.generate", title: "Draft planned content", executor: "codex.procedure", status: "active", definition: {public_discovery: !hidden, human_review: {eligible: true}, input_schema: {type: "object", properties: {}}}};
  const approvalLabel = hidden ? "Publish now" : "Approve draft";
  const first = {id: "first", project_id: "project", workflow_id: "article", workflow_name: "codex.procedure", status: "needs_input", review_required: true, review_version: 1, artifact_path: "content/drafts/first.md", canonical_commit_sha: "a".repeat(40), created_at: "2026-09-14T12:00:00Z"};
  const second = {...first, id: "second", review_source_run_id: "first", review_root_run_id: "first", review_version: 2, artifact_path: "content/drafts/second.md", canonical_commit_sha: "b".repeat(40)};
  let current = first, runs = [first], failSubmission = true, lagRunProjection = false;
  const projected = run => ({...run, status: lagRunProjection && run.id === "second" ? "pending" : run.status});
  const writes = [], errors = [], fileReads = [];
  const feedback = "Keep the opening; explain the missing mechanism using notes/product.md.";
  const buttonStyle = async button => {
    // Refreshing the run projection can replace a button between locator resolution
    // and evaluation. Detached nodes have empty computed styles; read its replacement.
    for (let attempt = 0; attempt < 20; attempt++) {
      const styles = await button.evaluate(node => {
        if (!node.isConnected) return null;
        const css = getComputedStyle(node);
        return Object.fromEntries(["backgroundColor", "color", "borderRadius", "padding", "fontSize", "fontWeight", "lineHeight"].map(key => [key, css[key]]));
      });
      if (styles) return styles;
      await button.page().waitForTimeout(25);
    }
    assert.fail("Approval button did not remain attached long enough to read its styles");
  };
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|chat|activity|decisions|files|file|document\/[^/]+|task\/[^/]+|compare\/[^/]+)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "");
      response.setHeader("Content-Type", "text/html"); return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const body = await fs.readFile(file); response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"); return response.end(body);} catch {response.writeHead(404); return response.end();}
    }
    if (request.method === "POST") {
      let text = ""; for await (const chunk of request) text += chunk;
      const body = JSON.parse(text || "{}"); writes.push({path: url.pathname, body});
      if (url.pathname.endsWith("/request-changes")) {
        if (failSubmission) {failSubmission = false; response.statusCode = 402; return send({detail: "Not enough credits"});}
        first.status = "superseded"; second.status = "pending"; runs = [second, first]; current = second; return send(second);
      }
      if (url.pathname.endsWith("/approve") || url.pathname.endsWith("/apply")) {second.status = "running"; return send(second);}
      return send({});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send(hidden ? [] : [workflow]);
    if (url.pathname === "/api/workflows/article") return send(workflow);
    if (url.pathname.endsWith("/integrations")) return send(hidden ? [{key: "infra.github", connection_id: "github", status: "connected", configuration: {selected_repository: "example/site"}}] : []);
    if (url.pathname === "/api/projects/project/runs") return send(runs.map(projected));
    if (url.pathname.endsWith("/decisions")) return send(current.status === "needs_input" ? [{id: "first", run_id: current.id, project_id: "project", workflow_key: workflow.key, workflow_title: workflow.title, kind: "review", title: "Review the draft", explanation: "The article is ready for review.", feedback_supported: true, items: [{file: current.artifact_path, revision: current.canonical_commit_sha, title: "An article with a purpose"}], created_at: first.created_at}] : []);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: current.status === "pending" ? 1 : 0, waiting_count: current.status === "needs_input" ? 1 : 0, runs_this_month: runs.length});
    if (url.pathname.endsWith("/files")) {fileReads.push(url.pathname); return send({revision: "c".repeat(40), files: [{path: "notes/product.md"}]});}
    const id = url.pathname.split("/")[4], run = id === "second" ? second : first;
    if (url.pathname.endsWith("/review/compare")) return send({previous: {run_id: "first", content: "# Clear title\n\nKeep the opening.\nA long list of frameworks.\n"}, revised: {run_id: "second", content: "# Clear title\n\nKeep the opening.\nA specific missing mechanism.\n"}});
    if (url.pathname.endsWith("/review")) return send({run_id: id, current_run_id: current.id, version: run.review_version, status: run.status, is_current: id === current.id,
      can_request_changes: id === current.id && run.status === "needs_input", can_approve: id === current.id && run.status === "needs_input", review_token: (id === "first" ? "1" : "2").repeat(64),
      artifact: {run_id: id, assessment: false}, versions: [current, ...(current.id === "second" ? [first] : [])], change_summary: id === "second" ? "Kept the opening and replaced the list with a useful mechanism." : null});
    if (url.pathname.endsWith("/artifact/document")) return send({filename: run.artifact_path.split("/").at(-1), word_count: 100, reading_minutes: 1, html: '<h1 id="title">An article with a purpose</h1><p>Keep the opening. Explain the useful mechanism.</p><h2 id="next">What happens next</h2><p>Clear copy for the reader, without generation notes.</p>', related_documents: []});
    if (/\/api\/workflows\/runs\/[^/]+$/.test(url.pathname)) return send(projected(run));
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(value => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic-test-only"}};
      localStorage.setItem("tin-lite:theme", value);
    }, theme);
    const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/?project=project#decisions`);
    await page.getByRole("button", {name: "Request changes", exact: true}).waitFor();
    assert.equal(await page.evaluate(() => state.workflows.some(item => item.id === "article")), !hidden);
    if (hidden) await page.getByRole("button", {name: "Open a pull request", exact: true}).waitFor();
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      const spacing = await page.locator(".decision-detail-card").evaluate(card => ({
        gap: card.querySelector("footer").getBoundingClientRect().top - card.querySelector(".decision-outputs").getBoundingClientRect().bottom,
        padding: parseFloat(getComputedStyle(card.querySelector(".decision-detail-body")).paddingBottom),
        emptyReviewDisplay: getComputedStyle(card.querySelector(".workflow-review")).display,
      }));
      assert.equal(spacing.emptyReviewDisplay, "none");
      assert.equal(spacing.gap, spacing.padding);
      await page.getByRole("button", {name: "Request changes", exact: true}).click();
      assert.equal(await page.locator(".review-composer").isVisible(), true);
      await page.getByRole("button", {name: "Close feedback", exact: true}).click();
      assert.equal(await page.locator(".workflow-review").isVisible(), false);
      if (process.env.TIN_REVIEW_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_REVIEW_SCREENSHOTS}/decision-spacing-${theme}-${width}.png`, fullPage: true});
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(`${base}/?project=project#document/first?return=decisions`);
    await page.getByRole("button", {name: "Request changes", exact: true}).waitFor();
    const readerApproval = page.getByRole("button", {name: approvalLabel, exact: true});
    const approvalStyle = await buttonStyle(readerApproval);
    await readerApproval.hover();
    await page.waitForTimeout(150);
    const approvalHoverStyle = await buttonStyle(readerApproval);
    assert.notEqual(approvalStyle.backgroundColor, approvalHoverStyle.backgroundColor);
    await page.mouse.move(0, 0);
    assert.equal(await page.locator(".review-composer").count(), 0);
    await page.getByRole("button", {name: "Request changes", exact: true}).click();
    await page.getByLabel("What should change?", {exact: true}).fill(feedback);
    assert.equal(await page.getByRole("button", {name: approvalLabel, exact: true}).isVisible(), false);
    await page.getByRole("button", {name: "Close feedback", exact: true}).click();
    assert.equal(await page.getByRole("button", {name: approvalLabel, exact: true}).isVisible(), true);
    await page.getByRole("button", {name: "Request changes", exact: true}).click();
    assert.equal(await page.getByLabel("What should change?", {exact: true}).inputValue(), feedback);
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.locator(".markdown-document .review-composer").count(), 0);
      assert.doesNotMatch(await page.locator(".review-composer").innerText(), /Up to \$|review the new draft before delivery|Add reference files|Add file/);
      assert.equal(await page.locator('.review-composer select, .review-composer [role="combobox"], .review-composer details, .review-composer input[type="file"]').count(), 0);
      assert.equal(await page.locator(".review-composer textarea").count(), 1);
      if (process.env.TIN_REVIEW_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_REVIEW_SCREENSHOTS}/review-${theme}-${width}.png`, fullPage: true});
    }
    await page.getByRole("button", {name: "Revise draft", exact: true}).click();
    await page.getByText("Not enough credits", {exact: true}).waitFor();
    await page.getByRole("button", {name: "Revise draft", exact: true}).click();
    await page.getByRole("heading", {name: "System", exact: true}).waitFor();
    const submissions = writes.filter(w => w.path.endsWith("/request-changes"));
    assert.equal(submissions.length, 2); assert.equal(submissions[0].body.request_id, submissions[1].body.request_id);
    assert.equal(submissions[1].body.feedback, feedback);
    assert.equal(Object.hasOwn(submissions[1].body, "reference_files"), false);
    assert.deepEqual(fileReads, []);
    second.status = "needs_input";
    lagRunProjection = true; // The exact review is ready before the initial run-list response.
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(`${base}/?project=project#decisions`);
    const cardApproval = page.locator(".decision-approval[data-apply-decision]");
    await cardApproval.waitFor();
    assert.deepEqual(await buttonStyle(cardApproval), approvalStyle);
    await cardApproval.hover();
    await page.waitForTimeout(150);
    assert.deepEqual(await buttonStyle(cardApproval), approvalHoverStyle);
    await page.mouse.move(0, 0);
    await page.waitForTimeout(150);
    if (process.env.TIN_REVIEW_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_REVIEW_SCREENSHOTS}/decision-approval-${theme}.png`, fullPage: true});
    await page.getByRole("button", {name: "Request changes", exact: true}).click();
    assert.equal(await page.locator(".decision-detail-card .review-composer").count(), 1);
    assert.doesNotMatch(await page.locator(".review-composer").innerText(), /Add reference files|Add file/);
    assert.deepEqual(fileReads, []);
    await page.getByRole("button", {name: "Read →", exact: true}).click();
    await page.getByRole("heading", {name: "An article with a purpose", exact: true}).waitFor();
    assert.equal(await page.locator(".review-composer").count(), 0);
    await page.getByRole("button", {name: "Compare", exact: true}).click();
    await page.getByText("Previous version → Revised version", {exact: true}).waitFor();
    assert.doesNotMatch(await page.locator(".review-comparison").innerText(), /Keep current|Use saved result|would be removed/);
    await page.getByRole("button", {name: "Close comparison", exact: true}).click();
    if (process.env.TIN_REVIEW_SCREENSHOTS && hidden) await page.screenshot({path: `${process.env.TIN_REVIEW_SCREENSHOTS}/hidden-article-${theme}.png`, fullPage: true});
    await page.getByRole("button", {name: hidden ? "Open a pull request" : approvalLabel, exact: true}).click();
    await page.waitForTimeout(100);
    assert.equal(writes.find(w => w.path.endsWith("/approve"))?.body.review_token, "2".repeat(64));
    if (hidden) assert.equal(writes.find(w => w.path.endsWith("/approve"))?.body.delivery, "github_pr");
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});

for (const conflict of [null, "DESIGN.md changed during review; no documents were applied."]) test(`reviewed document pair: ${conflict ? "conflict" : "exact approval"}`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.setContent('<main><div class="markdown-context-bar"><button class="markdown-context-action">Use these documents</button></div><article class="markdown-document"><h1>Brand</h1></article></main>');
    await page.addScriptTag({path: path.join(assets, "workflow-review.js")});
    await page.evaluate(conflict => {
      window.TinWorkflowReview.mount(document.querySelector("main"), {
        projectId: "project", runId: "pair", reader: true,
        api: async () => ({is_current: true, can_approve: !conflict, can_request_changes: false,
          review_token: "d".repeat(64), conflict, documents: [
            {destination: "brand/BRAND.md", change: "new"},
            {destination: "DESIGN.md", change: "unchanged"},
          ]}),
      });
    }, conflict);
    await page.getByText("brand/BRAND.md: new · DESIGN.md: carried forward unchanged", {exact: true}).waitFor();
    assert.equal(await page.getByRole("button", {name: "Request changes"}).count(), 0);
    assert.equal(await page.getByRole("button", {name: "Use these documents"}).isVisible(), !conflict);
    assert.equal(await page.evaluate(() => window.TinWorkflowReview.token("project", "pair")), "d".repeat(64));
    if (conflict) await page.getByText(conflict, {exact: true}).waitFor();
  } finally {await browser.close();}
});

test("reviewed pair shows a safe palette in the reader and Decisions approval", async () => {
  const project = {id: "project", name: "Example project", workspace_id: "workspace", workspace_name: "Example workspace", memory: {}};
  const workflow = {id: "documents", key: "example.documents", title: "Capture documents", executor: "codex.procedure", status: "active", definition: {human_review: {eligible: true}, procedure: {output: {apply_on_approval: {primary: "brand/BRAND.md", companion: "DESIGN.md"}}}, input_schema: {type: "object", properties: {}}}};
  const run = {id: "pair", project_id: "project", workflow_id: "documents", workflow_name: "example.documents", status: "needs_input", review_required: true, review_version: 1, artifact_path: "brand/proposals/pair/BRAND.md", canonical_commit_sha: "a".repeat(40), created_at: "2026-09-24T12:00:00Z"};
  const writes = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (!url.pathname.startsWith("/api/") && !url.pathname.startsWith("/assets/")) {
      response.setHeader("Content-Type", "text/html");
      return response.end((await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", ""));
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const raw = await fs.readFile(file); response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"); return response.end(raw);} catch {return response.writeHead(404).end();}
    }
    if (request.method === "POST") {
      let raw = ""; for await (const chunk of request) raw += chunk;
      writes.push({path: url.pathname, body: JSON.parse(raw || "{}")}); return send({...run, status: "running"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname.endsWith("/runs")) return send([run]);
    if (url.pathname.endsWith("/runs/pair")) return send(run);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 0, running_count: 0, waiting_count: 1, runs_this_month: 1, timezone: "UTC"});
    if (url.pathname.endsWith("/decisions")) return send([{id: "decision", run_id: run.id, project_id: "project", workflow_key: workflow.key, workflow_title: workflow.title, kind: "review", title: "Review your documents", explanation: "Both documents are ready for review.", feedback_supported: false, items: [{file: run.artifact_path, revision: run.canonical_commit_sha, title: "Brand and design"}], created_at: run.created_at}]);
    if (url.pathname.endsWith("/review")) return send({is_current: true, status: run.status, current_run_id: run.id, version: 1, can_approve: true, can_request_changes: false, review_token: "b".repeat(64), palette_preview: {paper: "#FAF8F0", ink: "#182B24", accent: "#287A55", signal: "url(https://example.invalid/track)"}, documents: [{destination: "brand/BRAND.md", change: "new"}, {destination: "DESIGN.md", change: "unchanged"}]});
    if (url.pathname.endsWith("/artifact/document")) return send({filename: "BRAND.md", word_count: 25, reading_minutes: 1, html: '<h1 id="brand">Example identity</h1><p>Warm paper, precise typography and a quiet green accent.</p><h2 id="generation">Generation rules</h2><p>Keep the founder’s green. Give each composition generous space.</p>', related_documents: [{label: "Design", path: "brand/proposals/pair/DESIGN.md", revision: run.canonical_commit_sha, url: "/file?project=project&path=brand%2Fproposals%2Fpair%2FDESIGN.md"}]});
    return send([]);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`, browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1100, height: 760}});
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic"}}; localStorage.setItem("tin-lite:theme", "light");});
    const page = await context.newPage();
    await page.goto(`${base}/?project=project#document/pair?return=decisions`);
    await page.getByText("brand/BRAND.md: new · DESIGN.md: carried forward unchanged", {exact: true}).waitFor();
    await page.getByRole("link", {name: "Design", exact: true}).waitFor();
    await page.getByRole("button", {name: "Use documents", exact: true}).waitFor();
    await page.getByText("accent #287A55", {exact: true}).waitFor();
    assert.equal(await page.locator(".review-palette i").count(), 3);
    assert.equal(await page.locator(".review-palette").getByText(/signal/).count(), 0);
    if (process.env.TIN_REVIEW_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_REVIEW_SCREENSHOTS}/reviewed-documents.png`, fullPage: true});
    await page.goto(`${base}/?project=project#decisions`);
    await page.getByText("brand/BRAND.md: new · DESIGN.md: carried forward unchanged", {exact: true}).waitFor();
    await page.locator("[data-apply-decision]").click();
    await page.waitForTimeout(100);
    assert.equal(writes.find(item => item.path.endsWith("/apply"))?.body.review_token, "b".repeat(64));
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});
