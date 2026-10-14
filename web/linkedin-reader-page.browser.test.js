import assert from "node:assert/strict";
import test from "node:test";
import {chromium} from "playwright";
import {serveApp, openApp, revision} from "./app-fixture.js";

const draftPath = "social/linkedin/drafts/2026-10-13-launch.json";
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");
const initial = {schema_version: "tin.social.linkedin_draft.v1", posts: [
  {id: "p1", text: "A recurring marketing task does not need an agent to make every decision.", target: "urn:li:organization:42", readiness: "ready", support: [], editor_notes: "Angle: Code first\nFormat: Tool tips", attachments: [], missing_assets: []},
  {id: "p2", text: "Write down the steps you already know.", target: "", readiness: "needs_evidence", support: [], editor_notes: "", attachments: [], missing_assets: []},
], gaps: [{angle: "", reason: "A measured outcome, a comparison or a customer observation."}]};
const accounts = [{id: "a2", urn: "urn:li:organization:42", kind: "organization", name: "Example Co"}];

async function setup(browser, base, viewport) {
  const opened = await openApp(browser, base, {viewport, url: "/files"});
  let saved = structuredClone(initial);
  const calls = [];
  await opened.page.route(url => new URL(url).pathname.startsWith("/api/projects/project/"), async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (path.endsWith("/linkedin/drafts")) {
      calls.push({path, method: request.method()});
      if (request.method() === "PUT") saved = structuredClone(request.postDataJSON().draft);
      return route.fulfill({json: {path: draftPath, revision, draft: saved, sha256: "s", connected: true, accounts, unavailable: [], publications: {}}});
    }
    if (path.endsWith("/linkedin/preview")) {
      calls.push({path, method: "POST"});
      const item = saved.posts.find(entry => entry.id === request.postDataJSON().post_id);
      return route.fulfill({json: {preview_token: "token", text: item.text, account: accounts[0], attachments: item.attachments, valid_until: new Date(Date.now() + 900000).toISOString()}});
    }
    if (path.endsWith("/files/upload")) {
      calls.push({path, method: "POST", type: request.headers()["content-type"], token: request.headers().authorization});
      return route.fulfill({json: {path: url.searchParams.get("path"), revision}});
    }
    if (path.endsWith("/files/raw")) return route.fulfill({body: PIXEL, contentType: "image/png"});
    return route.continue();
  });
  const address = `${base}/files?${new URLSearchParams({project: "project", x_draft: draftPath, x_back: "system"})}`;
  await opened.page.goto(address);
  await opened.page.locator(".li-reader .li-copy").waitFor();
  return {...opened, calls, address};
}

test("LinkedIn reader follows dashboard navigation, keeps unsaved edits and uploads through Files", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors, calls, address} = await setup(browser, base);
    assert.equal(await page.locator(".nav-item.is-active").getAttribute("data-view"), "files");
    assert.equal(await page.locator(".li-return").innerText(), "← My system");
    await page.locator(".li-bar").getByRole("button", {name: "Edit post", exact: true}).click();
    await page.locator(".li-text").fill("An edit kept while I check the other post.");
    await page.locator('[data-li-upload]').last().setInputFiles({name: "shot.png", mimeType: "image/png", buffer: PIXEL});
    await page.waitForFunction(() => document.querySelectorAll(".li-row[data-li-row]").length === 1);
    const upload = calls.find(call => call.path.endsWith("/files/upload"));
    assert.equal(upload.type, "image/png");
    assert.equal(upload.token, "Bearer synthetic");
    await page.locator('.li-rail-item[data-post="1"]').click();
    assert.match(await page.locator(".li-text").inputValue(), /Write down the steps/);
    await page.locator(".li-return").click();
    await page.waitForURL(url => new URL(url).pathname === "/system");
    assert.equal(await page.locator(".li-reader").count(), 0);
    await page.goBack();
    await page.locator(".li-reader .li-column").waitFor();
    await page.locator('.li-rail-item[data-post="0"]').click();
    assert.equal(await page.locator(".li-text").inputValue(), "An edit kept while I check the other post.");
    await page.locator(".li-bar").getByRole("button", {name: "Save changes"}).click();
    await page.locator(".li-bar").getByRole("button", {name: "Preview post"}).click();
    await page.locator("[data-li-exact]").waitFor();
    // A preview never survives leaving the reader.
    await page.locator(".li-return").click();
    await page.goBack();
    await page.locator(".li-reader .li-copy").waitFor();
    assert.equal(await page.locator("[data-li-exact]").count(), 0);
    await page.reload();
    await page.locator(".li-reader .li-copy").waitFor();
    assert.equal(page.url(), address);
    assert.equal(calls.filter(call => call.method === "PUT").length, 1);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});

test("LinkedIn reader stays within the screen and follows the active theme", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await setup(browser, base, {width: 1440, height: 1000});
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => TinTheme.apply(value), theme);
      for (const width of [1440, 768, 390]) {
        await page.setViewportSize({width, height: 1000});
        await page.evaluate(() => document.fonts.ready);
        const layout = await page.evaluate(() => {
          const reader = document.querySelector(".li-reader").getBoundingClientRect();
          const column = document.querySelector(".li-column").getBoundingClientRect();
          const rail = document.querySelector(".li-rail");
          return {
            overflow: document.documentElement.scrollWidth > innerWidth,
            column: column.left >= reader.left && column.right <= reader.right,
            rail: getComputedStyle(rail).display !== "none" ? rail.getBoundingClientRect().right <= column.left : true,
            background: getComputedStyle(document.querySelector(".li-reader")).backgroundColor,
          };
        });
        assert.equal(layout.overflow, false, `${theme} ${width}`);
        assert.equal(layout.column, true, `${theme} ${width}`);
        assert.equal(layout.rail, true, `${theme} ${width}`);
        assert.equal(layout.background, theme === "dark" ? "rgb(20, 18, 16)" : "rgb(247, 245, 239)");
      }
    }
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});
