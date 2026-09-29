// Packaged dashboard, synthetic HTTP only. No live credentials or projects.
// A decision card shows what it asks you to approve in plain words, names the exact copy an
// approval uses, holds a draft back while a revision of it waits, and the address bar keeps
// only the project once sign-in or a connection callback has finished.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const minutesAgo = minutes => new Date(Date.now() - minutes * 60_000).toISOString();

async function serve({draft: revision} = {}) {
  const project = {id: "project-1", name: "Example project", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const task = {
    id: "5a7e0000-0000-4000-8000-000000000001", project_id: project.id, workflow_id: "task", workflow_name: "project.task",
    status: "needs_input", task_phase: "review", task_title: "Update the FAQ", task_has_changes: true, review_required: false,
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
  if (revision !== undefined) {
    // An answer page draft saved two days ago, with GitHub ready to publish it.
    const draft = {id: "d7af0000-0000-4000-8000-000000000003", project_id: project.id, workflow_id: "answer-page", workflow_name: "content.answer_page",
      status: "needs_input", review_required: true, artifact_path: "reports/ANSWER_PAGE.md", canonical_commit_sha: "a".repeat(40), created_at: minutesAgo(2 * 24 * 60)};
    runs.push(draft);
    Object.assign(integrations[0], {connection_id: "github-1", status: "connected", project_id: project.id, configuration: {selected_repository: "example/site"}});
    decisions.splice(0, decisions.length, {id: "draft-decision", run_id: draft.id, project_id: project.id, workflow_key: "content.answer_page",
      workflow_title: "Draft an answer page", kind: "review", title: "Review: Which tools work with coding agents?", explanation: "Answer page draft is ready for your review.",
      consequence: "", items: [{file: draft.artifact_path, revision: draft.canonical_commit_sha, title: "ANSWER_PAGE.md"}], created_at: draft.created_at,
      version_saved_at: draft.created_at,
      revision: revision && {run_id: task.id, title: "Revise answer page draft", state: revision, revision: revision === "applied" ? "c".repeat(40) : null, at: minutesAgo(1)}});
  }
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = value => {response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|activity|decisions|integrations|integrations\/callback\/github|document\/[^/]+|task\/[^/]+)?$/.test(url.pathname)) {
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
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, waiting_count: 2, runs_this_month: 2});
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

test("decision card shows a task's proposed changes and plain labels", async () => {
  const {server, task, base} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.waitFor();
    // The list names each decision; the card subtitle reads as words, IDs only on hover.
    assert.deepEqual(await page.locator(".decision-list-item strong").allTextContents(), ["Review: Update the FAQ", "Review: Research a question deeply"]);
    const subtitle = card.locator(":scope > header code");
    assert.equal(await subtitle.textContent(), "Update the FAQ · Waiting 5m");
    assert.match(await subtitle.getAttribute("title"), /^project\.task · run_5a7e$/);
    // The changes are on the card, with the same controls the task page offers.
    assert.equal(await card.locator(".task-diff h2").textContent(), "Review the proposed files");
    assert.equal(await card.locator(".task-review-files strong").allTextContents().then(names => names.join()), "faq.md");
    assert.equal(await card.locator(".task-diff > header code").textContent(), "2 files · +12 −3");
    await card.locator(".task-exact-diff summary").click();
    assert.match(await card.locator(".task-diff-files").textContent(), /\+New answer/);
    assert.equal(await card.getByText("Open the task to review its proposed changes.").count(), 0);
    assert.equal(await card.getByRole("button", {name: "Open task →", exact: true}).count(), 1);
    assert.equal(await card.getByRole("button", {name: "Approve changes", exact: true}).count(), 1);
    assert.equal(await page.getByText("Observe", {exact: false}).count(), 0);
    await card.getByRole("button", {name: "Open document", exact: true}).click();
    await page.waitForURL(`**/document/${task.id}?**`);
    const documentUrl = new URL(page.url());
    assert.equal(documentUrl.searchParams.get("taskPath"), "docs/faq.md");

    // A review with nothing attached names the one link it offers.
    await page.goto(`${base}/decisions?project=project-1`);
    await page.locator('[data-decision-id="report-decision"]').click();
    assert.equal(await subtitle.textContent(), "Just arrived");
    assert.equal(await card.getByRole("button", {name: "Open run →", exact: true}).count(), 1);
    assert.equal(await card.locator(".decision-no-output").textContent(), "Open the run to review its output.");
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
  const {server, task, writes, base} = await serve({draft: "waiting"});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base, "/decisions?project=project-1");
    const card = page.locator(".decision-detail-card");
    await card.locator(".decision-revision").waitFor();
    assert.equal(
      (await card.locator(".decision-revision").textContent()).replace(/\s+/g, " ").trim(),
      "A revision of this draft is waiting in “Revise answer page draft”. Review it first. Review the revision →",
    );
    // The approval names the exact copy it would use; a two-day-old copy shows its date.
    assert.match(await card.locator(".decision-version").textContent(), /^Approves the draft from [A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2}\.$/);
    for (const name of ["Publish now", "Open a pull request"]) {
      assert.equal(await card.getByRole("button", {name, exact: true}).isDisabled(), true);
    }
    await card.getByRole("button", {name: "Publish now", exact: true}).click({force: true});
    assert.deepEqual(writes.filter(item => item.path.includes("/apply")), [], "a disabled approval must not reach the server");
    await card.getByRole("button", {name: "Review the revision →", exact: true}).click();
    await page.waitForURL(`**/task/${task.id}**`);
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
    await card.locator(".decision-revision").waitFor();
    assert.match(await card.locator(".decision-revision").textContent(), /“Revise answer page draft” revised this draft after it was saved, so publishing here would send the older copy\./);
    assert.equal(await card.getByRole("button", {name: "Publish now", exact: true}).count(), 0);
    assert.equal(await card.getByRole("button", {name: "Open a pull request", exact: true}).count(), 0);
    assert.match(await card.locator(".decision-version").textContent(), /^Keeps the draft from .+ in Tin\.$/);
    await card.getByRole("button", {name: "Keep in Tin", exact: true}).click();
    await page.getByText("Draft kept in Tin. Nothing was published.", {exact: true}).waitFor();
    const [approval] = writes.filter(item => item.path.includes("/apply"));
    assert.equal(approval.body.delivery, "none");
    await page.goto(`${base}/decisions?project=project-1`);
    await card.getByRole("button", {name: "Open the revised copy →", exact: true}).click();
    await page.waitForURL("**/file?**");
    const file = new URL(page.url());
    assert.equal(file.searchParams.get("path"), "reports/ANSWER_PAGE.md");
    assert.equal(file.searchParams.get("revision"), "c".repeat(40));
    assert.deepEqual(errors, []);
    await context.close();
  } finally { await browser.close(); server.close(); }
});
