// Packaged dashboard, synthetic HTTP only. No live credentials or projects.
// A decision card says in plain sentences what it asks you to approve, with no file rows or
// diffs; its one-line footer names the exact copy an approval uses and holds a draft back while
// a revision of it waits. Proposals can be discarded, and the menu badge, the list and System's
// "need you" count the same items. The address bar keeps only the project once sign-in or a
// connection callback has finished.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const minutesAgo = minutes => new Date(Date.now() - minutes * 60_000).toISOString();

async function serve({draft: revision, release, extra = [], waitingTasks = false} = {}) {
  const project = {id: "project-1", name: "Example project", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const task = {
    id: "5a7e0000-0000-4000-8000-000000000001", project_id: project.id, workflow_id: "task", workflow_name: "project.task",
    status: "needs_input", task_phase: "review", task_title: "Update the FAQ", task_has_changes: true, review_required: false,
    task_summary: "Rewrites the pricing answer in the FAQ. Adds a diagram of the plans. Removes a claim the site no longer makes. Updates the date.",
    review_requested_at: minutesAgo(5), created_at: minutesAgo(9),
    task_diff: {stats: {files: 2, additions: 12, deletions: 3}, files: [
      {path: "docs/faq.md", state: "modified", patch: "@@ -1 +1 @@\n-Old answer\n+New answer"},
      {path: "assets/diagram.svg", state: "added", patch: "<svg/>"},
    ]},
  };
  const report = {id: "4e90000-0000-4000-8000-000000000002", project_id: project.id, workflow_id: "report", workflow_name: "codex.procedure",
    status: "needs_input", review_required: true, created_at: minutesAgo(0)};
  const decisions = [
    {id: "task-decision", run_id: task.id, project_id: project.id, workflow_key: "project.task", workflow_title: "One-off project task",
      kind: "review", title: "Review: Update the FAQ", explanation: "Review the proposed project changes before they are applied.",
      consequence: "", items: [], created_at: minutesAgo(5)},
    {id: "report-decision", run_id: report.id, project_id: project.id, workflow_key: "research.deep_dive", workflow_title: "Research a question deeply",
      kind: "review", title: "Review: Research a question deeply", explanation: "Research is ready for your review.",
      consequence: "", items: [], created_at: minutesAgo(0)},
  ];
  const integrations = [{key: "infra.github", name: "GitHub", badge: "GH", description: "Repositories", unlocks: [], configured: true, status: "available", connection_id: null}];
  const writes = [];
  const runs = [task, report];
  if (release) {
    // A release announcement, as saved now (with its heading and first line) or by an older version.
    const announcement = {id: "7e1e0000-0000-4000-8000-000000000004", project_id: project.id, workflow_id: "release", workflow_name: "workflow.code",
      status: "needs_input", review_required: true, artifact_path: "reports/RELEASE_ANNOUNCE.md", canonical_commit_sha: "e".repeat(40), created_at: minutesAgo(60)};
    runs.push(announcement);
    decisions.splice(0, decisions.length, {id: "release-decision", run_id: announcement.id, project_id: project.id, workflow_key: "content.release_announce",
      workflow_title: "Announce a new release", kind: "review", consequence: "", created_at: announcement.created_at, version_saved_at: announcement.created_at,
      items: [{file: announcement.artifact_path, revision: announcement.canonical_commit_sha, title: "RELEASE_ANNOUNCE.md"}],
      ...(release === "saved"
        ? {title: "Review: Release announcements for Tin", output_title: "Release announcements for Tin", explanation: "What shipped: Public workflow packages anyone can contribute."}
        : {title: "Review: Tin release: brand guides, a public Registry", output_title: "Tin release: brand guides, a public Registry",
          explanation: "Announces six features and one improvement, with drafts for X, LinkedIn and your newsletter."})});
  }
  if (revision !== undefined) {
    // An answer page draft saved two days ago, with GitHub ready to publish it.
    const draft = {id: "d7af0000-0000-4000-8000-000000000003", project_id: project.id, workflow_id: "answer-page", workflow_name: "content.answer_page",
      status: "needs_input", review_required: true, artifact_path: "content/answers/which-tools.md", canonical_commit_sha: "a".repeat(40), created_at: minutesAgo(2 * 24 * 60)};
    runs.push(draft);
    Object.assign(integrations[0], {connection_id: "github-1", status: "connected", project_id: project.id, configuration: {selected_repository: "example/site"}});
    decisions.splice(0, decisions.length, {id: "draft-decision", run_id: draft.id, project_id: project.id, workflow_key: "content.answer_page",
      workflow_title: "Draft an answer page", kind: "review", title: "Review: Which tools work with coding agents?", output_title: "Which tools work with coding agents?",
      explanation: "Most marketing tools reach coding agents through an MCP server or a command-line tool.",
      consequence: "", items: [{file: draft.artifact_path, revision: draft.canonical_commit_sha, title: "which-tools.md"}], created_at: draft.created_at,
      version_saved_at: "2026-09-28T22:50:00Z",
      revision: revision && {run_id: task.id, title: "Revise answer page draft", state: revision, revision: revision === "applied" ? "c".repeat(40) : null, at: minutesAgo(1)}});
  }
  for (const item of extra) {
    runs.push(item.run);
    if (item.decision) decisions.push(item.decision);
  }
  if (waitingTasks) {
    // Neither is something to approve: a task asking a question, and a reviewed task that
    // changed nothing.
    runs.push(
      {id: "5a7e0000-0000-4000-8000-00000000000a", project_id: project.id, workflow_id: "task", workflow_name: "project.task", status: "needs_input",
        task_phase: "needs_input", task_title: "Pick a tone", task_question: "Which tone should the FAQ use?", review_required: false, created_at: minutesAgo(3)},
      {id: "5a7e0000-0000-4000-8000-00000000000b", project_id: project.id, workflow_id: "task", workflow_name: "project.task", status: "needs_input",
        task_phase: "review", task_title: "Check the links", task_has_changes: false, review_required: false, task_diff: {files: []}, created_at: minutesAgo(4)},
    );
  }
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|activity|decisions|files|integrations|integrations\/callback\/github|document\/[^/]+|task\/[^/]+)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "");
      response.setHeader("Content-Type", "text/html"); return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const body = await fs.readFile(file); response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"); return response.end(body);} catch {response.writeHead(404); return response.end();}
    }
    if (request.method !== "GET") {
      let raw = ""; for await (const chunk of request) raw += chunk;
      writes.push({path: url.pathname, body: JSON.parse(raw || "{}")});
      if (url.pathname.endsWith("/apply")) {
        const decision = decisions.find(item => url.pathname === `/api/decisions/${item.id}/apply`);
        const run = runs.find(item => item.id === decision?.run_id);
        if (!decision || !run) {response.statusCode = 404; return send({detail: "decision not found"});}
        decisions.splice(decisions.indexOf(decision), 1);
        return send({...run, status: "stopped", review_decision: JSON.parse(raw || "{}").action === "decline" ? "declined" : "approved"});
      }
      if (url.pathname === "/api/integrations/github/complete") {
        Object.assign(integrations[0], {connection_id: "github-1", status: "connected", project_id: project.id, configuration: {}});
        return send(integrations[0]);
      }
      return send({});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (/\/api\/projects\/[^/]+\/workflows$/.test(url.pathname)) return send([{id: "saved", workflow_key: "research.deep_dive", status: "active"}]);
    if (/\/api\/projects\/[^/]+\/runs$/.test(url.pathname)) return send(runs);
    if (url.pathname.endsWith("/decisions")) return send(decisions);
    if (url.pathname.endsWith("/integrations")) return send(integrations);
    if (url.pathname.endsWith("/infra.github/options")) return send([{id: "repo-1", label: "example/site"}]);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, waiting_count: decisions.length, runs_this_month: 2, timezone: "UTC"});
    const saved = runs.find(item => url.pathname === `/api/workflows/runs/${item.id}/artifact/document`);
    if (saved) {
      return send({markdown: "# Which tools work with coding agents?", html: "<h1 id=\"title\">Which tools work with coding agents?</h1><p>Most use MCP.</p>",
        filename: saved.artifact_path.split("/").at(-1), path: saved.artifact_path, revision: saved.canonical_commit_sha,
        source_url: `/api/workflows/runs/${saved.id}/artifact`, timestamp: saved.created_at, word_count: 8, reading_minutes: 1, headings: [], related_documents: []});
    }
    if (url.pathname.endsWith("/files")) return send({revision: "a".repeat(40), files: [{path: "content/answers/which-tools.md"}, {path: "content/answers/other.md"}]});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, task, writes, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base, url) {
  const errors = [];
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(() => {
    window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic-test-only"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", "light");
  });
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(base + url);
  return {page, context, errors};
}

test("a task's decision describes the change in a few sentences, with no diff on the card", async () => {
  const {server, task, base} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.waitFor();
    // The list names each decision; the card subtitle reads as words, IDs only on hover.
    assert.deepEqual(await page.locator(".decision-list-item strong").allTextContents(), ["Review: Update the FAQ", "Review: Research a question deeply"]);
    const subtitle = card.locator(":scope > header code");
    assert.equal(await card.locator(":scope > header strong").textContent(), "Update the FAQ");
    assert.equal(await subtitle.textContent(), "One-off project task · Waiting 5m");
    assert.match(await subtitle.getAttribute("title"), /^project\.task · run_5a7e$/);
    // The body is the task's own summary, three sentences at most; the diff stays on the task page.
    assert.equal(await card.locator(".decision-detail-body").innerText(), "Rewrites the pricing answer in the FAQ. Adds a diagram of the plans. Removes a claim the site no longer makes.");
    assert.equal(await card.locator(".task-diff, .task-review-files, pre, table, ul, ol").count(), 0);
    assert.match(await card.locator(".decision-version").textContent(), /^Changes from [A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2}$/);
    assert.equal(await card.getByRole("button", {name: "Approve changes", exact: true}).count(), 1);
    assert.equal(await page.getByText("Observe", {exact: false}).count(), 0);
    await card.getByRole("button", {name: "Open task →", exact: true}).click();
    await page.waitForURL(`**/task/${task.id}**`);

    // A review with nothing attached offers its one link and no filler sentence.
    await page.goto(`${base}/decisions?project=project-1`);
    await page.locator('[data-decision-id="report-decision"]').click();
    assert.equal(await card.locator(":scope > header strong").textContent(), "Research a question deeply");
    assert.equal(await subtitle.textContent(), "Just arrived");
    // "<workflow> is ready for your review." only repeated the title, so it is left out.
    assert.equal(await card.locator(".decision-summary").count(), 0);
    assert.equal(await card.getByRole("button", {name: "Open run →", exact: true}).count(), 1);
    assert.equal(await card.locator(".decision-detail-body > *").count(), 0);
    assert.equal(await card.getByRole("button", {name: "Approve", exact: true}).count(), 1);
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("the address bar keeps only the project after a connection callback and while moving around", async () => {
  const {server, base} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const callback = "/integrations/callback/github?code=synthetic&state=synthetic&installation_id=7&setup_action=install&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth";
    const {page, context, errors} = await open(browser, base, callback);
    await page.getByText("example/site", {exact: true}).waitFor();
    let current = new URL(page.url());
    assert.equal(current.pathname, "/integrations");
    assert.deepEqual([...current.searchParams.keys()], ["project"]);
    await page.keyboard.press("Escape");
    await context.close();

    // A leftover from an earlier sign-in does not follow the person to the next page.
    const again = await open(browser, base, "/system?project=project-1&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth");
    await again.page.locator('.nav-item[data-view="decisions"]').click();
    await again.page.locator(".decision-detail-card").waitFor();
    current = new URL(again.page.url());
    assert.equal(current.pathname, "/decisions");
    assert.deepEqual([...current.searchParams.entries()], [["project", "project-1"]]);
    assert.deepEqual([...errors, ...again.errors], []);
    await again.context.close();
  } finally { await browser.close(); server.close(); }
});

test("a draft with a waiting revision cannot be approved until the revision is resolved", async () => {
  const {server, writes, base} = await serve({draft: "waiting"});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.locator(".decision-version").waitFor();
    // One short footer line says a newer revision waits.
    assert.equal(await card.locator("footer > span").innerText(), "A newer revision is waiting");
    assert.equal(await card.locator(".decision-detail-body").innerText(), "Most marketing tools reach coding agents through an MCP server or a command-line tool.");
    // Every approval looks blocked, not only the primary one, and nothing can be remembered.
    const looks = await card.locator("footer").evaluate(footer => [...footer.querySelectorAll("[data-apply-decision]")].map(button => {
      const style = getComputedStyle(button);
      return {name: button.textContent, disabled: button.disabled, color: style.color, background: style.backgroundColor, cursor: style.cursor};
    }));
    assert.deepEqual(looks, [
      {name: "Open a pull request", disabled: true, color: "rgba(38, 35, 26, 0.35)", background: "rgba(0, 0, 0, 0)", cursor: "not-allowed"},
      {name: "Publish now", disabled: true, color: "rgba(38, 35, 26, 0.35)", background: "rgba(38, 35, 26, 0.08)", cursor: "not-allowed"},
    ]);
    assert.equal(await card.getByText("Do this for future drafts").count(), 0);
    await card.getByRole("button", {name: "Publish now", exact: true}).click({force: true});
    assert.deepEqual(writes.filter(item => item.path.includes("/apply")), [], "a disabled approval must not reach the server");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("after a revision is applied the older draft can only stay in Tin", async () => {
  const {server, writes, base} = await serve({draft: "applied"});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.locator(".decision-version").waitFor();
    assert.equal(await card.locator("footer > span").innerText(), "A newer revision was applied");
    assert.equal(await card.getByRole("button", {name: "Publish now", exact: true}).count(), 0);
    assert.equal(await card.getByRole("button", {name: "Open a pull request", exact: true}).count(), 0);
    await card.getByRole("button", {name: "Keep in Tin", exact: true}).click();
    await page.getByText("Draft kept in Tin. Nothing was published.", {exact: true}).waitFor();
    const [approval] = writes.filter(item => item.path.includes("/apply"));
    assert.equal(approval.body.delivery, "none");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("a release announcement card says what the draft is in one sentence, without repeating itself", async () => {
  for (const release of ["saved", "older"]) {
    const {server, base} = await serve({release});
    const browser = await chromium.launch({headless: true});
    try {
      const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
      const card = page.locator(".decision-detail-card");
      await card.waitFor();
      const text = async selector => (await card.locator(selector).allTextContents()).map(item => item.trim());
      const [title] = await text(":scope > header strong");
      const [subtitle] = await text(":scope > header code");
      const summary = await text(".decision-summary");
      // Prose only: no file box, notice or list in the body, and one link to the draft.
      assert.equal(await card.locator(".decision-detail-body > *").count(), summary.length);
      assert.equal(await card.getByRole("button", {name: "Open draft →", exact: true}).count(), 1);
      if (release === "saved") {
        assert.equal(title, "Release announcements for Tin");
        assert.equal(subtitle, "Announce a new release · Waiting 1h");
        assert.deepEqual(summary, ["What shipped: Public workflow packages anyone can contribute."]);
      } else {
        // Saved before outputs carried their heading, then named from its own file: the card
        // counts what it announces instead of standing empty under the workflow's name.
        assert.equal(title, "Tin release: brand guides, a public Registry");
        assert.equal(subtitle, "Announce a new release · Waiting 1h");
        assert.deepEqual(summary, ["Announces six features and one improvement, with drafts for X, LinkedIn and your newsletter."]);
      }
      const shown = [title, subtitle, ...summary, ...(await text(".decision-version"))];
      assert.equal(new Set(shown.map(item => item.toLowerCase())).size, shown.length, `repeated text: ${shown.join(" | ")}`);
      assert.deepEqual(errors, []);
      await context.close();
    } finally { await browser.close(); server.close(); }
  }
});

const projectId = "project-1";
function reviewRun(id, fields) {
  return {id, project_id: projectId, status: "needs_input", review_required: true, canonical_commit_sha: "b".repeat(40), created_at: minutesAgo(120), ...fields};
}

test("an output without a heading is named by its workflow and day, and a task that changed nothing says so", async () => {
  const batch = reviewRun("5b0c0000-0000-4000-8000-000000000005", {workflow_id: "batch", workflow_name: "workflow.code", artifact_path: "social/batches/2026-09-29.md"});
  const tidy = {id: "5a7e0000-0000-4000-8000-000000000006", project_id: projectId, workflow_id: "task", workflow_name: "project.task", status: "needs_input",
    task_phase: "review", task_title: "Tidy the FAQ", task_has_changes: null, review_required: false, task_diff: {files: []}, review_requested_at: minutesAgo(30), created_at: minutesAgo(40)};
  const {server, base} = await serve({extra: [
    {run: batch, decision: {id: "batch-decision", run_id: batch.id, project_id: projectId, workflow_key: "social.post_batch", workflow_title: "Social post batch",
      kind: "review", title: "Review: Social post batch · Sep 29", output_title: "Social post batch · Sep 29", explanation: "Review the complete output before this workflow continues.",
      consequence: "", items: [{file: batch.artifact_path, revision: batch.canonical_commit_sha, title: "2026-09-29.md"}], created_at: batch.created_at, version_saved_at: "2026-09-29T09:05:00Z"}},
    {run: tidy, decision: {id: "tidy-decision", run_id: tidy.id, project_id: projectId, workflow_key: "project.task", workflow_title: "One-off project task",
      kind: "review", title: "Review: Tidy the FAQ", output_title: "Tidy the FAQ", explanation: "Review the proposed project changes before they are applied.",
      consequence: "", items: [], created_at: tidy.review_requested_at, version_saved_at: tidy.review_requested_at}},
  ]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.waitFor();
    await page.locator('[data-decision-id="batch-decision"]').click();
    assert.equal(await page.locator('[data-decision-id="batch-decision"] strong').textContent(), "Review: Social post batch · Sep 29");
    // The title already names the workflow, so the subtitle does not repeat it.
    assert.equal(await card.locator(":scope > header strong").textContent(), "Social post batch · Sep 29");
    assert.equal(await card.locator(":scope > header code").textContent(), "Waiting 2h");
    assert.equal(await card.locator("footer > span").innerText(), "Draft from Sep 29, 09:05");

    await page.locator('[data-decision-id="tidy-decision"]').click();
    assert.equal(await card.locator(":scope > header strong").textContent(), "Tidy the FAQ");
    assert.equal(await card.locator(".decision-detail-body").innerText(), "Finished without changing any files.");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("the footer is one short line, and a draft opened from Decisions shows its folder path", async () => {
  const {server, base} = await serve({draft: null});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.locator(".decision-version").waitFor();
    const footer = await card.locator("footer > span").evaluate(line => ({text: line.innerText, height: line.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(line).lineHeight)}));
    assert.equal(footer.text, "Draft from Sep 28, 22:50");
    assert.equal(footer.height, footer.lineHeight);
    assert.equal(await card.getByText("Do this for future drafts").count(), 1);

    await card.getByRole("button", {name: "Open draft →", exact: true}).click();
    const crumbs = page.locator("nav.markdown-filename");
    await crumbs.waitFor();
    // The same clickable path as the Files file view: folders open Files, the name is text.
    assert.deepEqual(await crumbs.locator("button").allTextContents(), ["content", "answers"]);
    assert.equal(await crumbs.locator('[aria-current="page"]').textContent(), "which-tools.md");
    await crumbs.getByRole("button", {name: "answers", exact: true}).click();
    await page.waitForURL(url => new URL(url).pathname === "/files");
    assert.equal(await page.evaluate(() => state.filesDirectory), "content/answers");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("a proposal can be discarded: it leaves Decisions and every count, the guide stays", async () => {
  const style = reviewRun("57e10000-0000-4000-8000-000000000007", {workflow_id: "style", workflow_name: "style.capture", artifact_path: "style/proposals/2026-09-29-writing-style.md", created_at: minutesAgo(5)});
  const {server, writes, base} = await serve({extra: [{run: style, decision: {id: "style-decision", run_id: style.id, project_id: projectId, workflow_key: "style.capture",
    workflow_title: "Capture writing style", kind: "review", title: "Review: Writing style guide for Example project", output_title: "Writing style guide for Example project",
    explanation: "Proposes short, direct sentences that name the file and the next step.", consequence: "",
    items: [{file: style.artifact_path, revision: style.canonical_commit_sha, title: "2026-09-29-writing-style.md"}], created_at: style.created_at, version_saved_at: "2026-09-29T12:10:00Z"}}]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.waitFor();
    assert.equal(await page.locator("#decision-count").textContent(), "3");
    await page.locator('[data-decision-id="style-decision"]').click();
    assert.equal(await card.locator("footer > span").innerText(), "Proposal from Sep 29, 12:10");
    assert.deepEqual(await card.locator("footer button").allTextContents(), ["Discard", "Not now", "Approve guide"]);
    assert.equal(await card.getByRole("button", {name: "Discard", exact: true}).evaluate(button => getComputedStyle(button).color), "rgb(236, 75, 26)");

    await card.getByRole("button", {name: "Discard", exact: true}).click();
    await page.getByText("Proposal discarded. The current guide is unchanged.", {exact: true}).waitFor();
    assert.deepEqual(writes.filter(item => item.path.endsWith("/apply")).map(item => [item.path, item.body]), [["/api/decisions/style-decision/apply", {action: "decline"}]]);
    assert.equal(await page.locator('[data-decision-id="style-decision"]').count(), 0);
    assert.equal(await page.locator("#decision-count").textContent(), "2");
    assert.match(await page.locator("#project-summary").textContent(), / · 2 need you$/);
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});

test("the menu badge, the Decisions list and System's count agree", async () => {
  const {server, base} = await serve({waitingTasks: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    await page.locator(".decision-detail-card").waitFor();
    // A task asking a question and a reviewed task that changed nothing are in neither count.
    assert.equal(await page.locator(".decision-list-item").count(), 2);
    assert.equal(await page.locator("#decision-count").textContent(), "2");
    assert.equal(await page.locator("#project-summary").textContent(), "Example · 0 running · 2 need you");
    await page.locator('.nav-item[data-view="workflows"]').click();
    await page.locator(".system-view").waitFor();
    assert.equal(await page.locator("#project-summary").textContent(), "Example · 0 running · 2 need you");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});
