import assert from "node:assert/strict";
import test from "node:test";
import {chromium} from "playwright";

const accounts = [
  {id: "a2", urn: "urn:li:organization:42", kind: "organization", name: "Example Co"},
  {id: "a1", urn: "urn:li:person:AbC123", kind: "personal", name: "Sam Founder"},
];
const post = (id, extra = {}) => ({id, text: "We shipped the thing.", target: "", readiness: "ready", support: [], editor_notes: "", attachments: [], missing_assets: [], ...extra});
const initial = {schema_version: "tin.social.linkedin_draft.v1", posts: [post("p1")]};
const image = (n, alt = "") => ({type: "image", path: `social/linkedin/media/run/${n}.png`, alt_text: alt});
// One transparent pixel, so images decode and report their size.
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

async function openReader(page, draft, {view = {}, files = [], width} = {}) {
  if (width) await page.setViewportSize({width, height: 900});
  await page.route("http://localhost/linkedin-post-test", route => route.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
  await page.goto("http://localhost/linkedin-post-test");
  await page.addStyleTag({path: "src/tin_lite/static/app.css"});
  await page.addStyleTag({path: "src/tin_lite/static/linkedin-posts.css"});
  await page.addScriptTag({path: "src/tin_lite/static/linkedin-posts.js"});
  await page.evaluate(({draft, accounts, view, files, pixel}) => {
    window.requests = [];
    window.saved = structuredClone(draft);
    window.revision = "a".repeat(40);
    window.publications = view.publications || {};
    window.uploads = [];
    window.mockApi = async (path, options = {}) => {
      const body = options.body && JSON.parse(options.body);
      requests.push({path, method: options.method || "GET", body});
      if (path.includes("/linkedin/drafts") && !options.method) return {path: "social/linkedin/drafts/2026-10-13-d.json", revision, draft: structuredClone(saved), sha256: "s", connected: view.connected ?? true, accounts: view.accounts || accounts, unavailable: view.unavailable || [], publications};
      if (path.endsWith("/linkedin/drafts") && options.method === "PUT") {
        saved = structuredClone(body.draft); revision = "b".repeat(40);
        return {path: body.path, revision, draft: structuredClone(saved), sha256: "t"};
      }
      if (path.endsWith("/linkedin/preview")) {
        const item = saved.posts.find(entry => entry.id === body.post_id);
        return {preview_token: "exact-token", text: item.text, account: accounts.find(entry => entry.urn === item.target), attachments: item.attachments, valid_until: new Date(Date.now() + 15 * 60000).toISOString()};
      }
      if (path.endsWith("/linkedin/publish")) return {id: "delivery-id", run_id: "delivery-id", status: "pending", url: null};
      if (path.includes("/linkedin/publications")) return {publications};
      if (path.includes("/files?include_modified=true")) return {revision, files};
      throw new Error(`Unexpected ${path}`);
    };
    window.mockRawFetch = async path => {
      requests.push({path, method: "GET"});
      return new Response(new Blob([Uint8Array.from(atob(pixel), c => c.charCodeAt(0))], {type: "image/png"}));
    };
    window.mockUpload = async (path, file, {onProgress}) => {
      uploads.push({path, type: file.type, size: file.size});
      onProgress?.(file.size / 2);
      if (window.failNextUpload) {window.failNextUpload = false; throw new Error("The upload didn't reach Tin.");}
      return {path: new URL(path, location.href).searchParams.get("path"), revision: "c".repeat(40)};
    };
    TinLinkedInPosts.open({projectId: "project-one", runId: "run-one", path: "social/linkedin/drafts/2026-10-13-d.json", returnLabel: "my system", api: mockApi, rawFetch: mockRawFetch, upload: mockUpload, connect: () => {window.connected = true;}});
  }, {draft, accounts, view, files, pixel: PIXEL.toString("base64")});
  await page.locator(".li-reader .li-column").waitFor({state: "attached"});
  await page.locator(".li-reader .li-copy, .li-reader .li-gaps, .li-reader .li-card").first().waitFor();
}

const bar = page => page.locator(".li-bar");
const writes = page => page.evaluate(() => requests.filter(item => item.method !== "GET").map(item => item.path));

test("LinkedIn reader chooses who posts, edits in place, previews the exact post and follows its publication", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await openReader(page, {...initial, posts: [post("p1"), post("p2", {readiness: "needs_evidence", text: "Another post."})]});
    assert.equal(await page.locator(".li-reader").getAttribute("aria-label"), "LinkedIn post drafts");
    assert.equal(await page.locator(".li-bar-title").textContent(), "2026-10-13 · Post 1 of 2");
    assert.deepEqual(await page.locator(".li-rail-item small").allInnerTexts(), ["Ready", "Needs evidence"]);
    // Two accounts and none chosen: nothing says who posts until one is picked.
    await page.getByText("Who posts isn't chosen yet.").waitFor();
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    assert.match(await page.locator(".li-rail-item[aria-current] small").textContent(), /Editing/);
    await page.locator(".li-picker").click();
    // Your own profile first, then the project's pages.
    assert.deepEqual(await page.locator(".li-menu button").allInnerTexts(), ["Sam Founder\nprofile", "Example Co\npage"]);
    await page.locator(".li-menu button", {hasText: "Example Co"}).click();
    assert.match(await page.locator(".li-picker").innerText(), /Example Co\s+page/);
    const text = page.locator(".li-text");
    assert.equal(await text.getAttribute("maxlength"), "3000");
    await text.fill("We shipped the thing, and wrote down how.");
    assert.equal(await page.locator("[data-li-count]").textContent(), "41 / 3,000");
    await bar(page).getByText("Unsaved changes").waitFor();
    await bar(page).getByRole("button", {name: "Save changes"}).click();
    await page.locator(".li-copy").getByText("wrote down how").waitFor();
    await page.getByText("Posting as Example Co").waitFor();
    await bar(page).getByRole("button", {name: "Preview post"}).click();
    const exact = page.locator("[data-li-exact]");
    await exact.waitFor();
    assert.equal(await exact.getAttribute("aria-label"), "Exact LinkedIn preview");
    assert.match(await exact.innerText(), /Example Co\s+Page · now\s+We shipped the thing, and wrote down how\./);
    await page.getByText("This is exactly what LinkedIn receives: the text and the account.").waitFor();
    await bar(page).getByText("Preview good for 15 minutes").waitFor();
    await bar(page).getByRole("button", {name: "Publish this exact post"}).click();
    await bar(page).getByText("Publishing to LinkedIn…").waitFor();
    await page.getByText("Sending the post as the Example Co page.", {exact: false}).waitFor();
    assert.equal(await bar(page).getByRole("button", {name: "Publish this exact post"}).isDisabled(), true);
    await page.evaluate(() => {publications = {p1: {post_id: "p1", run_id: "delivery-id", state: "published", url: "https://www.linkedin.com/feed/update/urn:li:share:1", at: new Date().toISOString(), account: {urn: "urn:li:organization:42", kind: "organization", name: "Example Co"}, text: saved.posts[0].text, message: null}};});
    const view = bar(page).getByRole("link", {name: "View on LinkedIn ↗"});
    await view.waitFor({timeout: 10000});
    assert.equal(await view.getAttribute("href"), "https://www.linkedin.com/feed/update/urn:li:share:1");
    assert.equal(await page.locator(".li-rail-item[aria-current] small").textContent(), "Published");
    await bar(page).getByRole("button", {name: "Back to drafts"}).click();
    await bar(page).getByRole("button", {name: "Preview post"}).waitFor();
    assert.deepEqual(await writes(page), [
      "/api/projects/project-one/linkedin/drafts",
      "/api/projects/project-one/linkedin/preview",
      "/api/projects/project-one/linkedin/publish",
    ]);
    const sent = await page.evaluate(() => requests.filter(item => item.method !== "GET"));
    assert.equal(sent[0].body.draft.posts[0].target, "urn:li:organization:42");
    assert.equal(sent[2].body.preview_token, "exact-token");
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});

test("Image rows upload to LinkedIn's media folder, refuse what LinkedIn can't take before sending, and stop at 20", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    await openReader(page, {...initial, posts: [post("p1", {target: "urn:li:person:AbC123", attachments: [image(1, "The dashboard")]})]});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    assert.equal(await page.locator('[data-li-upload]').first().getAttribute("accept"), "image/png,image/jpeg");
    assert.match(await page.locator(".li-images header").innerText(), /Images\s+1 of 20/);
    await page.locator("[data-li-facts]").first().getByText("1.png · 1 × 1").waitFor();
    await page.locator('[data-li-upload]').last().setInputFiles([
      {name: "deck.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(10)},
      {name: "huge.png", mimeType: "image/png", buffer: Buffer.alloc(5_000_001)},
      {name: `${"launch hero image final ".repeat(5)}v3 (1).png`, mimeType: "image/png", buffer: PIXEL},
      {name: "photo.jpg", mimeType: "image/png", buffer: PIXEL},
    ]);
    await page.getByText("LinkedIn posts from Tin take PNG or JPEG images.", {exact: false}).waitFor();
    await page.getByText("Images can be 5 MB at most.", {exact: false}).waitFor();
    await page.waitForFunction(() => uploads.length === 2 && document.querySelectorAll(".li-row[data-li-row]").length === 3);
    const paths = await page.evaluate(() => uploads.map(item => new URL(item.path, location.href).searchParams.get("path")));
    // The extension follows the file's type, whatever the name says, and a long name keeps it.
    assert.match(paths[0], /^social\/linkedin\/media\/run-one\/[0-9a-f-]{36}-launch-hero-image-final-.{0,60}\.png$/);
    assert.match(paths[1], /-photo\.png$/);
    // Refused files stay with their reason until removed; nothing was sent for them.
    assert.equal(await page.locator(".li-row.is-refused").count(), 2);
    while (await page.locator(".li-row.is-refused").count()) await page.locator(".li-row.is-refused .li-remove").first().click();
    assert.equal(await page.locator(".li-row.is-refused").count(), 0);
    await page.evaluate(() => {failNextUpload = true;});
    await page.locator('[data-li-upload]').last().setInputFiles({name: "later.png", mimeType: "image/png", buffer: PIXEL});
    await page.getByText("Tin couldn't store this image. Your connection may have dropped.").waitFor();
    await page.getByRole("button", {name: "Try again"}).click();
    await page.waitForFunction(() => document.querySelectorAll(".li-row[data-li-row]").length === 4);
    await page.locator('[data-li-alt="1"]').fill("The launch hero");
    await bar(page).getByRole("button", {name: "Save changes"}).click();
    await page.locator(".li-read-images figcaption").getByText("4 images · 2 described").waitFor();
    const twenty = Array.from({length: 20}, (_, n) => image(n));
    await openReader(page, {...initial, posts: [post("p1", {attachments: twenty})]});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByText("This post has 20 images, LinkedIn's limit. Remove one to add another.").waitFor();
    assert.equal(await page.locator("[data-li-upload]").count(), 0);
  } finally {await browser.close();}
});

test("Images already in Files are offered newest first, filtered by name, and added once", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const files = [
      {path: "notes.md", modified_at: "2026-10-14T00:00:00Z"},
      {path: "uploads/old.jpg", modified_at: "2026-10-01T00:00:00Z"},
      {path: "social/linkedin/media/run/1.png", modified_at: "2026-10-10T00:00:00Z"},
      {path: "brand/new-launch.png", modified_at: "2026-10-12T00:00:00Z"},
    ];
    await openReader(page, {...initial, posts: [post("p1", {attachments: [image(1)]})]}, {files});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByRole("button", {name: "choose one from Files"}).click();
    await page.locator(".li-file").first().waitFor();
    assert.deepEqual(await page.locator(".li-file-body > span:first-child").allInnerTexts(), ["new-launch.png", "1.png", "old.jpg"]);
    assert.deepEqual(await page.locator(".li-file-action").allInnerTexts(), ["Add", "Added", "Add"]);
    await page.locator(".li-filter").fill("launch");
    assert.deepEqual(await page.locator(".li-file-body > span:first-child").allInnerTexts(), ["new-launch.png"]);
    await page.locator(".li-file-action").getByRole("button", {name: "Add"}).click();
    assert.deepEqual(await page.locator(".li-file-action").allInnerTexts(), ["Added"]);
    await page.getByRole("button", {name: "Done"}).click();
    assert.equal(await page.locator(".li-row[data-li-row]").count(), 2);
    assert.match(await page.locator(".li-images header").innerText(), /2 of 20/);
  } finally {await browser.close();}
});

test("Who a post goes out as says what to do when there's no choice to make", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    await openReader(page, initial, {view: {connected: false, accounts: []}});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByText("LinkedIn posting isn't connected to this project yet.").waitFor();
    await page.getByRole("button", {name: "Connect LinkedIn"}).click();
    assert.equal(await page.evaluate(() => window.connected), true);
    // With one account there is nothing to choose.
    await openReader(page, initial, {view: {accounts: [accounts[0]]}});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    assert.equal(await page.locator(".li-picker").count(), 0);
    assert.match(await page.locator(".li-as").innerText(), /Posting as\s+Example Co\s+page/);
    // A draft aimed at another member's profile can be read and edited, not published as them.
    const theirs = {urn: "urn:li:person:Em", kind: "personal", name: "Emre Sample", reason: "another_member"};
    await openReader(page, {...initial, posts: [post("p1", {target: theirs.urn})]}, {view: {accounts: [accounts[0]], unavailable: [theirs]}});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByText("Only Emre can post as this profile.").waitFor();
    await page.getByRole("button", {name: "Post as Example Co instead"}).click();
    assert.match(await page.locator(".li-as").innerText(), /Posting as\s+Example Co\s+page/);
    // A page that left the connection keeps its place in the draft, named, until changed.
    const gone = {urn: "urn:li:organization:7", kind: "organization", name: "Old Page", reason: "disconnected"};
    await openReader(page, {...initial, posts: [post("p1", {target: gone.urn})]}, {view: {unavailable: [gone]}});
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.getByText("The Old Page page is no longer connected. Choose another account or reconnect it.").waitFor();
    assert.equal(await page.getByRole("button", {name: "Reconnect"}).count(), 1);
  } finally {await browser.close();}
});

test("A publication's outcome stays with its post: refused until it changes, uncertain until checked", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const account = {urn: "urn:li:organization:42", kind: "organization", name: "Example Co"};
    const publications = {
      p1: {post_id: "p1", run_id: "r1", state: "not_published", url: null, at: "2026-10-14T09:00:00Z", account, text: "We shipped the thing.", message: "LinkedIn refused this post's content, for example as a duplicate of an earlier post. Change it, then preview it again."},
      p2: {post_id: "p2", run_id: "r2", state: "uncertain", url: null, at: "2026-10-14T09:00:00Z", account, text: "Second.", message: "LinkedIn may have published this post."},
    };
    await openReader(page, {...initial, posts: [post("p1"), post("p2", {text: "Second."})]}, {view: {publications}});
    await page.getByText("LinkedIn refused this post's content", {exact: false}).waitFor();
    assert.equal(await bar(page).locator(".li-bar-note").textContent(), "Not published");
    await bar(page).getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator(".li-text").fill("We shipped the thing, again, differently.");
    await page.locator('.li-rail-item[data-post="1"]').click();
    const check = bar(page).getByRole("link", {name: "Open the Example Co page ↗"});
    await check.waitFor();
    assert.equal(await check.getAttribute("href"), "https://www.linkedin.com/company/42/");
    await page.getByText("Tin didn't hear back from LinkedIn, so it won't send this post again.", {exact: false}).waitFor();
    // Changed, the refused post reads as a draft again.
    assert.equal(await page.locator('.li-rail-item[data-post="0"] small').textContent(), "Ready");
  } finally {await browser.close();}
});

test("What a drafting run couldn't support has its own place in the rail, with or without posts", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}});
    const gaps = [{angle: "A customer result", reason: "Add a publishable customer result."}];
    await openReader(page, {...initial, gaps});
    assert.match(await page.locator('.li-rail-item[data-post="gaps"]').innerText(), /More context needed\s+1 note from this run/);
    await page.locator('.li-rail-item[data-post="gaps"]').click();
    await page.getByRole("heading", {name: "More context needed"}).waitFor();
    assert.match(await page.locator(".li-gaps").innerText(), /Add a publishable customer result\./);
    assert.equal(await page.locator(".li-bar-title").textContent(), "LinkedIn drafts · Oct 13");
    await openReader(page, {...initial, posts: [], gaps});
    await page.getByText("This run wrote no posts.", {exact: false}).waitFor();
    assert.equal(await page.getByRole("button", {name: "Preview post"}).count(), 0);
  } finally {await browser.close();}
});

test("At phone width the title lists the posts and the primary action sits at the bottom", async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    for (const theme of ["light", "dark"]) {
      await openReader(page, {...initial, posts: [post("p1", {target: "urn:li:person:AbC123", attachments: [image(1), image(2)]}), post("p2")]}, {width: 390});
      await page.evaluate(theme => {document.documentElement.dataset.theme = theme;}, theme);
      assert.equal(await page.locator(".li-rail").isVisible(), false);
      assert.equal(await page.locator(".li-dock").getByRole("button", {name: "Preview post"}).isVisible(), true);
      assert.equal(await bar(page).getByRole("button", {name: "Edit", exact: true}).isVisible(), true);
      await page.locator(".li-switch").click();
      assert.deepEqual(await page.locator(".li-switcher-item > span").allInnerTexts(), ["Post 1", "Post 2"]);
      await page.locator('.li-switcher-item[data-post="1"]').click();
      assert.match(await page.locator(".li-switch").innerText(), /Post 2 of 2/);
      for (const width of [390, 768, 1440]) {
        await page.setViewportSize({width, height: 900});
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${theme} ${width} overflows`);
      }
    }
  } finally {await browser.close();}
});
