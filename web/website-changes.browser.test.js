// Packaged dashboard, synthetic HTTP only. No live credentials or projects.
// Proposed website changes wait in Decisions beside run reviews: one card per change row, with
// its source, kind, pages or files and what it does, a note when a protected page makes it a
// pull request, and a button row of one-word controls. Approving or declining records the
// founder's decision on the exact content they read. Judgment calls show their question and
// options, with Tin's suggestion marked; the coding agent answers them, so they have no buttons.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const minutesAgo = minutes => new Date(Date.now() - minutes * 60_000).toISOString();
const sha = character => character.repeat(64);

function rows() {
  return [
    {change_id: "oa_" + "1".repeat(20), source: "audit", kind: "robots_sitemap_line", title: "Sitemap missing from robots.txt: adds a Sitemap line to robots.txt",
      paths: ["/robots.txt"], content_sha256: sha("a"), content_revision: null, status: "pending", proposed_at: minutesAgo(30),
      detail: {change: "adds a Sitemap line to robots.txt", check_id: "robots.sitemap_reference_missing"}, protected: null},
    {change_id: "oa_" + "2".repeat(20), source: "planned", kind: "noindex", title: "Page decisions proposes keeping /sign-in out of search: keeps /sign-in out of search (noindex)",
      paths: ["/sign-in"], content_sha256: sha("b"), content_revision: null, status: "pending", proposed_at: minutesAgo(20),
      detail: {planned: {from: "/sign-in", to: null, source: "organic.content_efficacy"}}, protected: "/sign-in"},
    {change_id: "bi_" + "3".repeat(20), source: "blog_index", kind: "index", title: "Blog index: A blog index that lists every post",
      paths: ["/blog"], content_sha256: sha("c"), content_revision: null, status: "pending", proposed_at: minutesAgo(10),
      detail: {summary: "A blog index that lists every post with its date.", route: "/blog",
        files: [{path: "src/app/blog/page.tsx", action: "create", bytes: 18}, {path: "src/lib/posts.ts", action: "update", bytes: 24}]}, protected: null},
  ];
}

const QUESTION = {
  id: "oa_" + "4".repeat(20), source: "audit", recorded_at: minutesAgo(5),
  question: "robots.txt blocks AI search crawlers. Should they be allowed?",
  finding: {check_id: "robots.ai_search_crawlers_blocked", issue: "AI search crawlers are blocked", urls: []},
  options: [{value: "allow", label: "Allow AI search crawlers"}, {value: "keep_blocked", label: "Keep them blocked"}],
  suggestion: "allow", why: "Blocking them keeps the site out of ChatGPT search answers.",
};

async function serve() {
  const project = {id: "project-1", name: "Example project", workspace_id: "ws", workspace_name: "Example", member_count: 1, hidden: false};
  const pending = rows();
  const writes = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = (value, status = 200) => {response.statusCode = status; response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(value));};
    if (/^\/(?:system|decisions)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8")).replaceAll("{{ASSET_VERSION}}", "test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "");
      response.setHeader("Content-Type", "text/html"); return response.end(html);
    }
    if (url.pathname.startsWith("/assets/")) {
      try {
        const file = path.join(assets, url.pathname.slice(8));
        const body = await fs.readFile(file);
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream");
        return response.end(body);
      } catch {response.writeHead(404); return response.end();}
    }
    if (request.method !== "GET") {
      let raw = ""; for await (const chunk of request) raw += chunk;
      writes.push({path: url.pathname, body: JSON.parse(raw || "{}")});
      const decided = url.pathname.match(/\/website-changes\/([^/]+)\/(approve|decline)$/);
      if (decided) {
        const row = pending.find(item => item.change_id === decided[1]);
        if (!row) return send({detail: "change not found"}, 404);
        pending.splice(pending.indexOf(row), 1);
        return send({...row, status: decided[2] === "approve" ? "approved" : "declined", decided_by: "member"});
      }
      return send({});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname.endsWith("/website-changes/questions")) return send({questions: [QUESTION]});
    if (url.pathname.endsWith("/website-changes")) return send(url.searchParams.get("status") === "pending" ? pending : []);
    if (url.pathname.endsWith("/system")) return send({workflow_count: 1, running_count: 0, waiting_count: 0, runs_this_month: 0, timezone: "UTC"});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, writes, pending, base: `http://127.0.0.1:${server.address().port}`};
}

async function open(browser, base) {
  const errors = [];
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
  await context.addInitScript(() => {
    window.Clerk = {load: async () => {}, isSignedIn: true, user: {id: "member", firstName: "QA"}, session: {getToken: async () => "synthetic-test-only"}, mountSignIn: () => {}};
    localStorage.setItem("tin-lite:theme", "light");
  });
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/decisions?project=project-1`);
  return {page, context, errors};
}

test("each proposed website change is a card with one-word controls", async () => {
  const {server, base} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base);
    const card = page.locator(".decision-detail-card");
    await card.waitFor({timeout: 5000});
    // Three change rows and one judgment call wait; the badge counts all four.
    assert.equal(await page.locator(".decision-list-item").count(), 4);
    assert.equal(await page.locator("#decision-count").textContent(), "4");
    assert.equal(await card.locator(":scope > header code").textContent(), "Audit fix · robots_sitemap_line");
    assert.equal(await card.locator(".decision-summary").textContent(), "Tin adds a Sitemap line to robots.txt.");
    assert.deepEqual(await card.locator(".website-change-paths code").allTextContents(), ["/robots.txt"]);
    assert.equal(await card.locator(".website-change-protected").count(), 0);
    // The button row holds controls only, one word each.
    assert.deepEqual(await card.locator("footer button").allTextContents(), ["Decline", "Approve"]);
    assert.equal(await card.locator("footer :not(button)").count(), 0);

    // A protected planned change says it opens a pull request even when approved.
    await page.locator(`[data-decision-id="change:oa_${"2".repeat(20)}"]`).click();
    assert.equal(await card.locator(":scope > header code").textContent(), "Planned URL change · noindex");
    assert.equal(await card.locator(".decision-summary").textContent(), "Keep /sign-in out of search (noindex).");
    assert.match(await card.locator(".website-change-protected").textContent(), /^Protected: \/sign-in opens a pull request for you to merge/);

    // The blog index plan lists its files with what happens to each.
    await page.locator(`[data-decision-id="change:bi_${"3".repeat(20)}"]`).click();
    assert.equal(await card.locator(":scope > header code").textContent(), "Blog index · index");
    assert.deepEqual(await card.locator(".website-change-files code").allTextContents(), ["src/app/blog/page.tsx", "src/lib/posts.ts"]);
    assert.deepEqual(await card.locator(".website-change-files span").allTextContents(), ["create", "update"]);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});

test("approving or declining records the decision on the content the founder read", async () => {
  const {server, base, writes} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base);
    const card = page.locator(".decision-detail-card");
    await card.waitFor({timeout: 5000});
    await card.getByRole("button", {name: "Approve", exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll(".decision-list-item").length === 3);
    const [approved] = writes;
    assert.equal(approved.path, `/api/projects/project-1/website-changes/oa_${"1".repeat(20)}/approve`);
    assert.equal(approved.body.content_sha256, sha("a"));
    assert.match(approved.body.request_id, /^[0-9a-f-]{36}$/);
    assert.match(await page.locator(".toast, [role=status]").last().textContent(), /Approved\./);
    // The next card is the protected change; declining it keeps it off the site.
    await card.getByRole("button", {name: "Decline", exact: true}).click();
    await page.waitForFunction(() => document.querySelectorAll(".decision-list-item").length === 2);
    assert.equal(writes[1].path, `/api/projects/project-1/website-changes/oa_${"2".repeat(20)}/decline`);
    assert.equal(writes[1].body.content_sha256, sha("b"));
    assert.equal(await page.locator("#decision-count").textContent(), "2");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});

test("a judgment call shows its question and options, with Tin's suggestion and no buttons", async () => {
  const {server, base, writes} = await serve();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await open(browser, base);
    await page.locator(".decision-detail-card").waitFor({timeout: 5000});
    await page.locator(`[data-decision-id="question:${QUESTION.id}"]`).click();
    const card = page.locator(".judgment-call-card");
    assert.equal(await card.locator(":scope > header strong").textContent(), QUESTION.question);
    assert.equal(await card.locator(":scope > header code").textContent(), "Judgment call · Audit fix");
    assert.deepEqual(await card.locator(".judgment-options strong").allTextContents(), ["Allow AI search crawlers", "Keep them blocked"]);
    assert.equal(await card.locator(".judgment-options li.is-suggested strong").textContent(), "Allow AI search crawlers");
    assert.equal(await card.locator(".judgment-suggested").textContent(), "Tin suggests");
    assert.equal(await card.locator("button").count(), 0);
    assert.match(await card.innerText(), /Your coding agent answers this with the next website change run/);
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});
