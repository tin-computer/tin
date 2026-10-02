// Real packaged My system page with synthetic HTTP. No provider calls or production writes.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const sourceIds = {audit: "audit", keyword: "keywords"};
const inputs = {project_id: "project", audit_run_id: sourceIds.audit, keyword_run_id: sourceIds.keyword,
  start_date: "2026-09-09", duration: "6_months", pieces_per_batch: 2, context_files: [], amendment_id: ""};
const schema = {type: "object", additionalProperties: false, properties: {
  project_id: {type: "string"}, audit_run_id: {type: "string"}, keyword_run_id: {type: "string"},
  start_date: {type: "string"}, duration: {type: "string", enum: ["2_weeks", "6_months"]},
  pieces_per_batch: {type: "integer"}, context_files: {type: "array", items: {type: "string"}}, amendment_id: {type: "string"},
}, required: ["project_id", "audit_run_id", "keyword_run_id", "start_date"]};
const titles = ["iMessage API for AI agents", "Two-way SMS for agent conversations", "Inbound messaging webhooks", "OpenClaw messaging setup", "Choosing a messaging channel", "Separate workspaces for agency clients"];

for (const theme of ["light", "dark"]) test(`packaged My system content card: ${theme}, responsive layout and scoped saves`, async () => {
  const project = {id: "project", name: "ClawMessenger", workspace_id: "workspace", workspace_name: "ClawMessenger", timezone: "Europe/Berlin", member_count: 2};
  const workflow = {id: "workflow", key: "content.plan", title: "Plan upcoming content", executor: "content.plan", status: "active", description: "Plan content from research.", definition: {input_schema: schema, schedule_modes: ["on_demand", "weekly"]}};
  let configured = {id: "program", project_id: "project", workflow_id: "workflow", name: "ClawMessenger — six-month content plan", workflow_key: "content.plan", workflow_title: workflow.title, workflow_description: workflow.description,
    inputs: structuredClone(inputs), input_schema: schema, schedule: null, status: "active", settings_revision: 1,
    run_count: 1, done_count: 1, failed_count: 0, last_run_status: "succeeded", last_run_id: "run", last_finished_at: "2026-09-09T05:36:00Z",
    last_result_summary: "Content roadmap. Nothing generated or published.", last_artifact_path: "reports/content-plan/run/PLAN.md"};
  let plan = {program_id: "program", host: "www.clawmessenger.com", start_date: inputs.start_date, end_date: "2027-03-09", strategy: "Evidence-backed buyer tasks.", batches: Array.from({length: 26}, (_, index) => ({
    id: `week_${String(index + 1).padStart(2, "0")}`, due_date: new Date(Date.UTC(2026, 8, 9 + 7 * index)).toISOString().slice(0, 10),
    items: index < titles.length ? [{id: `internal_item_${index}`, title: titles[index], brief: "Help developers evaluate the workflow using verified product documentation. Inspect the existing page before deciding what to update.", intent: "Developer evaluating messaging for an AI agent", action: "update_page", destination: "https://www.clawmessenger.com/docs", source_ids: ["keyword:source"], verification: ["Verify supported operations and test every example against current documentation."], readiness: "needs_verification"}] : [],
  }))};
  const writes = [], errors = [];
  let planReads = 0;
  let liveRuns = [];
  // An already-covered topic names the page that covers it (content.generate's no-draft result).
  let programDrafts = {internal_item_0: {run_id: "covered-run", status: "succeeded", stage: "already_covered", has_output: false,
    assessment: {outcome: "already_covered", rationale: "The SMS guide already answers this."}, covered_by: "https://www.clawmessenger.com/docs/sms"}};
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = (value, type = "application/json") => {response.setHeader("Content-Type", type); response.end(type === "application/json" ? JSON.stringify(value) : value);};
    if (url.pathname.startsWith("/assets/")) {
      const file = path.resolve(assets, url.pathname.slice(8));
      if (!file.startsWith(`${assets}/`)) {response.writeHead(404).end(); return;}
      try {send(await fs.readFile(file), file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : file.endsWith(".svg") ? "image/svg+xml" : "application/octet-stream");}
      catch {response.writeHead(404).end();} return;
    }
    if (["/", "/system"].includes(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replace(/<script\b[^>]*src="\{\{CLERK[^>]+>[\s\S]*?<\/script>/g, "").replaceAll("{{ASSET_VERSION}}", "test");
      return send(html, "text/html");
    }
    if (request.method === "PUT") {
      let body = ""; for await (const chunk of request) body += chunk;
      const payload = JSON.parse(body); writes.push({path: url.pathname, payload});
      if (url.pathname.endsWith("/plan")) {plan = payload.plan; return send({revision: "b".repeat(40)});}
      if (url.pathname.endsWith("/workflows/program")) {configured = {...configured, ...payload, settings_revision: configured.settings_revision + 1}; return send(configured);}
      response.writeHead(400); return send({detail: "Unexpected write"});
    }
    if (request.method !== "GET") {response.writeHead(400); return send({detail: "No execution is allowed in this UI test"});}
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
    if (url.pathname === "/api/projects/project/runs") return send(liveRuns);
    if (url.pathname === "/api/workflows/runs/running") return send(liveRuns[0]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, runs_this_month: 1, waiting_count: 0});
    if (url.pathname.endsWith("/content-programs/sources")) return send({sources: Object.entries(sourceIds).map(([kind, id]) => ({id, executor: kind === "audit" ? "organic.audit" : "organic.keyword_plan", site_url: "www.clawmessenger.com", market: "US", created_at: "2026-09-08T12:00:00Z"})), next_offset: null});
    if (url.pathname.endsWith("/content-programs/program/plan")) {planReads++; return send({revision: "a".repeat(40), plan});}
    if (url.pathname.endsWith("/content-programs/program/delivery")) return send({revision: "a".repeat(40), settings: {mode: "draft_only", repository: "", path_pattern: "content/blog/{slug}.md", frontmatter: {}, item_paths: {}}});
    if (url.pathname.endsWith("/content-programs/program")) return send({initialized: true, plan_path: "content/plans/program/plan.json", batches: [], pending_revision: null, drafts: programDrafts});
    if (url.pathname.endsWith("/files")) return send({files: [{path: "notes/product.md"}]});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(theme => {
      window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic-test-only"}};
      localStorage.setItem("tin-lite:theme", theme);
    }, theme);
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/#workflows`);
    await page.locator(".system-card-identity").waitFor();
    assert.match(await page.locator(".system-card-last").innerText(), /^Roadmap saved/);
    assert.equal(planReads, 0, "collapsed operational card must not read plan files");

    // A failure changes the status and recovery actions, not the card rhythm or Manual run treatment.
    const successfulCardHeight = (await page.locator(".system-workflow-card").boundingBox()).height;
    const successfulManualStyle = await page.getByRole("button", {name: "Manual run", exact: true}).evaluate(node => {
      const style = getComputedStyle(node);
      return {background: style.backgroundColor, color: style.color, fontWeight: style.fontWeight, height: style.height, padding: style.padding};
    });
    configured = {...configured, failed_count: 1, last_run_status: "failed", last_started_at: "2026-09-09T05:35:34Z"};
    await page.reload();
    await page.getByRole("button", {name: "Retry", exact: true}).waitFor();
    assert.equal((await page.locator(".system-workflow-card").boundingBox()).height, successfulCardHeight);
    assert.deepEqual(await page.getByRole("button", {name: "Manual run", exact: true}).evaluate(node => {
      const style = getComputedStyle(node);
      return {background: style.backgroundColor, color: style.color, fontWeight: style.fontWeight, height: style.height, padding: style.padding};
    }), successfulManualStyle);
    configured = {...configured, failed_count: 0, last_run_status: "succeeded", last_started_at: null};
    await page.reload();
    await page.locator(".system-card-identity").waitFor();

    await page.locator(".system-card-identity").click();
    await page.locator('[data-item-field="title"]').waitFor({state: "attached"});
    await page.getByText("Article delivery · Drafts in Tin", {exact: true}).waitFor();
    const covering = page.locator('.content-topic[data-topic-id="internal_item_0"] a[href="https://www.clawmessenger.com/docs/sms"]');
    await covering.waitFor({state: "attached"});
    assert.equal(await covering.textContent(), "/docs/sms");
    assert.match(await page.locator('.content-topic[data-topic-id="internal_item_0"] .system-config-note').textContent(), /Already covered\. The SMS guide already answers this\. Covered by \/docs\/sms; the next draft moves on\./);
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator("form form").count(), 0);
    assert.equal(await page.locator(".content-program-panel .content-batch-items fieldset").evaluate(node => getComputedStyle(node).borderTopWidth), "0px");
    assert.match(await page.locator(".content-program-panel").innerText(), /6 topics · 26 weeks · 20 weeks unplanned/);
    assert.doesNotMatch(await page.locator(".content-topic > summary").innerText(), /internal_item|needs_verification/);
    const work = await page.locator(".content-program-work").boundingBox(), when = await page.locator(".system-config-when").boundingBox();
    assert.ok(when.x > work.x && Math.abs(when.y - work.y) < 1);
    assert.equal(await page.locator(".content-program-work").evaluate(node => getComputedStyle(node).paddingLeft), "16px");
    if (process.env.TIN_CONTENT_PLAN_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_CONTENT_PLAN_SCREENSHOTS}/content-card-${theme}-1440.png`, fullPage: true});
    await page.locator('.content-topic > summary').click();
    if (process.env.TIN_CONTENT_PLAN_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_CONTENT_PLAN_SCREENSHOTS}/content-card-${theme}-editing.png`, fullPage: true});

    // Shared dropdown keyboard behavior, including long calendars, binds only once.
    const batch = page.getByRole("button", {name: "Batch", exact: true});
    await batch.focus(); await page.keyboard.press("ArrowDown"); await page.keyboard.press("End"); await page.keyboard.press("Enter");
    assert.equal(await page.locator("[data-batch-select]").inputValue(), "week_26");
    await batch.click(); await page.locator('[data-tin-select-value="week_01"]').click();
    const settings = page.locator(".content-program-settings");
    assert.equal(await settings.locator(".schedule-timed").first().isVisible(), false);
    await settings.getByRole("button", {name: "Weekly", exact: true}).click();
    assert.equal(await settings.locator(".schedule-timed").first().isVisible(), true);
    await settings.getByRole("button", {name: "On demand", exact: true}).click();
    assert.equal(await settings.locator(".schedule-timed").first().isVisible(), false);
    await settings.getByRole("button", {name: "Decrease Maximum pieces per batch"}).click();
    await page.evaluate(() => {bindTinControls(document.querySelector(".content-program-settings")); bindTinControls(document.querySelector(".content-program-settings"));});
    await settings.getByRole("button", {name: "Increase Maximum pieces per batch"}).click();
    assert.equal(await settings.locator('[name="input:pieces_per_batch"]').inputValue(), "2");

    // Enter in a topic saves the plan, not the settings form.
    await page.locator('[data-item-field="title"]').fill("A precise developer guide");
    await page.locator('[data-item-field="title"]').press("Enter");
    await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("Upcoming content saved."));
    assert.equal(writes.length, 1);
    assert.ok(writes[0].path.endsWith("/plan"));
    assert.equal(writes[0].payload.expected_revision, "a".repeat(40));
    assert.equal(plan.batches[0].items[0].title, "A precise developer guide");

    // Settings have a separate save; an unsaved topic survives close/reopen.
    await page.locator('[data-item-field="title"]').fill("Unsaved topic draft");
    await settings.getByRole("button", {name: "Save settings", exact: true}).click();
    await page.locator(".content-program-card").waitFor({state: "detached"});
    assert.equal(writes.length, 2);
    assert.ok(writes[1].path.endsWith("/workflows/program"));
    const {project_id: boundProject, ...editableInputs} = inputs;
    assert.equal(boundProject, project.id);
    assert.deepEqual(writes[1].payload.inputs, editableInputs);
    assert.equal(writes[1].payload.schedule, null, "saving settings must not enable a schedule");
    assert.equal(plan.batches[0].items[0].title, "A precise developer guide");
    await page.locator(".system-card-identity").click();
    assert.equal(await page.locator('[data-item-field="title"]').inputValue(), "Unsaved topic draft");

    // Run projections intentionally omit raw inputs. Delivery updates still refresh
    // the open program's facts without replacing its unsaved editorial changes.
    programDrafts = {internal_item_0: {run_id: "article", has_output: true, delivery: {status: "pending", repository: "owner/site", path: "posts/article.md"}}};
    liveRuns = [{id: "article", project_id: project.id, workflow_id: "00000000-0000-4000-8000-000000000031", workflow_name: "codex.procedure", status: "succeeded", created_at: new Date().toISOString(), content_delivery: programDrafts.internal_item_0.delivery}];
    await page.evaluate(() => pollRuns());
    await page.getByText("Drafts and pull requests · 1", {exact: true}).waitFor();
    programDrafts.internal_item_0.delivery = {...programDrafts.internal_item_0.delivery, status: "completed", pull_request: {number: 42, url: "https://github.com/owner/site/pull/42"}};
    liveRuns[0].content_delivery = programDrafts.internal_item_0.delivery;
    await page.evaluate(() => pollRuns());
    await page.locator('a[href="https://github.com/owner/site/pull/42"]').waitFor({state: "attached"});
    assert.equal(await page.locator('[data-item-field="title"]').inputValue(), "Unsaved topic draft");

    for (const width of [768, 390]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no horizontal overflow at ${width}`);
      if (width === 390) {const a = await page.locator(".content-program-work").boundingBox(), b = await page.locator(".system-config-when").boundingBox(); assert.ok(b.y > a.y + a.height - 1);}
      if (process.env.TIN_CONTENT_PLAN_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_CONTENT_PLAN_SCREENSHOTS}/content-card-${theme}-${width}.png`, fullPage: true});
    }
    liveRuns = [{id: "running", project_id: project.id, workflow_id: workflow.id, project_workflow_id: configured.id,
      workflow_name: workflow.key, workflow_title: workflow.title, status: "running", created_at: new Date().toISOString(),
      progress_mode: "steps", progress_current: 1, progress_total: 3, progress_summary: "Checking saved research", trigger_source: "manual"}];
    await page.setViewportSize({width: 1440, height: 1000});
    await page.reload();
    await page.locator(".system-workflow-card.is-running .system-card-identity").click();
    if (process.env.TIN_CONTENT_PLAN_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_CONTENT_PLAN_SCREENSHOTS}/content-card-${theme}-running.png`, fullPage: true});
    assert.equal(await page.getByRole("button", {name: "Stop planning", exact: true}).count(), 1);
    assert.equal(await page.getByRole("button", {name: "Manual run", exact: true}).isDisabled(), true);
    await page.getByRole("button", {name: "Open", exact: true}).click();
    await page.locator(".system-run-detail").waitFor();
    assert.equal(writes.length, 2, "observation must not start or stop a run");

    // New saves appear first immediately; editing or running an older card cannot move it.
    await page.evaluate(() => {
      const original = state.projectWorkflows[0];
      upsertProjectWorkflow({...original, id: "new-program", name: "Newest saved roadmap"});
      renderWorkflows();
    });
    assert.deepEqual(await page.locator(".system-card-identity strong").allTextContents(), ["Newest saved roadmap", configured.name]);
    await page.evaluate(() => {
      const original = state.projectWorkflows.find(item => item.id === "program");
      upsertProjectWorkflow({...original, name: "Older roadmap edited", updated_at: new Date().toISOString()});
      renderWorkflows();
    });
    assert.deepEqual(await page.locator(".system-card-identity strong").allTextContents(), ["Newest saved roadmap", "Older roadmap edited"]);
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});
