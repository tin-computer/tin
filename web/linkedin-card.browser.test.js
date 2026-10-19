// Packaged dashboard with synthetic APIs. No live credentials or projects.
// LinkedIn is one card in Integrations (Paper INVITE-B, INVITE-B3): posting (LinkedIn sign-in) and
// reading (Tin for Chrome, each member's own session) are rows of it, though they stay two
// connections underneath.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp } from "./app-fixture.js";

const soon = () => new Date(Date.now() + 86400000).toISOString();
const posting = (connected = true, status = "connected") => ({
  key: "social.linkedin", name: "LinkedIn posting", badge: "in", description: "Post approved drafts.", unlocks: ["LinkedIn posts"],
  configured: true, connection_id: connected ? "posting-1" : null, status: connected ? status : "available",
  configuration: connected ? {accounts: [
    {name: "Avery Example", kind: "person", active: true},
    {name: "Example Co", kind: "organization", active: true},
  ]} : {},
  connected_at: "2026-10-14T09:00:00Z",
});
const reading = (connected = true) => ({
  key: "network.linkedin", name: "LinkedIn", badge: "in", description: "Read posts and who engaged.", unlocks: ["People who engaged"],
  configured: true, connection_id: connected ? "reading-1" : null, status: connected ? "connected" : "available",
  configuration: connected ? {actor: {name: "Avery Example"}, collection_permission: {version: 1, mode: "cloud_preferred"},
    session_state: "available", session_expires_at: soon(), session_verified_at: new Date(Date.now() - 3600000).toISOString()} : {},
  connected_at: "2026-10-14T09:00:00Z",
});
const github = {key: "infra.github", name: "GitHub", badge: "GH", description: "Repositories", unlocks: ["Reviewable pull requests"], configured: true, connection_id: null, status: "available"};

async function open(integrations, run) {
  const served = await serveApp({decisionCount: 0, integrations});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, served.base, {url: "/integrations", theme: "dark"});
    await page.locator(".integration-list").waitFor();
    await run(page, served);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    served.server.close();
  }
}

const card = page => page.locator(".linkedin-card");
const counts = page => page.locator("[data-integration-filter]").allInnerTexts();

test("nothing connected: one card whose rows are the steps", {timeout: 120000}, async () => {
  await open([posting(false), github, reading(false)], async page => {
    assert.equal(await card(page).count(), 1);
    assert.equal(await page.getByText("LinkedIn posting", {exact: true}).count(), 0);
    assert.equal(await card(page).locator(".integration-key").innerText(), "posting · reading · sending");
    assert.deepEqual(await counts(page), ["All 3", "Connected 0", "Needs you 0", "Available 3"]);
    await card(page).getByRole("button", {name: "Connect", exact: true}).click();
    const rows = card(page).locator(".linkedin-part");
    assert.equal(await rows.count(), 2);
    assert.match(await rows.nth(0).innerText(), /Posting\s*Post drafts you approve, as you or your company page, and read their results\s*Sign in with LinkedIn/);
    assert.match(await rows.nth(1).innerText(), /Reading\s*Read your posts and who engaged with them, with your own session\s*Add Tin for Chrome/);
    assert.equal(await card(page).getByRole("button", {name: "Sign in with LinkedIn"}).getAttribute("data-integration-connect"), "social.linkedin");
    assert.equal(await card(page).getByRole("button", {name: "Add Tin for Chrome"}).getAttribute("data-integration-connect"), "network.linkedin");
    assert.equal(await card(page).locator(".integration-control-footer").count(), 0);
  });
});

test("partly connected: posting connected, reading still a step", {timeout: 120000}, async () => {
  await open([posting(), reading(false)], async page => {
    assert.deepEqual(await counts(page), ["All 2", "Connected 1", "Needs you 0", "Available 1"]);
    assert.equal(await card(page).locator(".integration-primary").innerText(), "Avery Example, Example Co");
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    const rows = card(page).locator(".linkedin-part");
    assert.match(await rows.nth(0).innerText(), /Posting\s*Avery Example, Example Co · approved posts only\s*LinkedIn sign-in · pages for everyone, your profile only for you\s*Manage/);
    assert.match(await rows.nth(1).innerText(), /Add Tin for Chrome/);
    // Manage opens the accounts and posting's own controls.
    await card(page).getByRole("button", {name: "Manage"}).click();
    const manage = card(page).locator(".linkedin-part-manage");
    assert.match(await manage.innerText(), /page Example Co/);
    assert.equal(await manage.getByRole("button", {name: "Add accounts"}).count(), 1);
    assert.equal(await manage.getByRole("button", {name: "Disconnect posting"}).count(), 1);
    // The main lines are words in the sans, the line under them a literal in mono.
    const fonts = await rows.nth(0).evaluate(row => [getComputedStyle(row.querySelector(".linkedin-part-main")).fontFamily, getComputedStyle(row.querySelector(".linkedin-part-meta")).fontFamily]);
    assert.doesNotMatch(fonts[0], /Mono/i);
    assert.match(fonts[1], /Mono/i);
  });
});

test("both connected: Turn off removes reading only, Disconnect LinkedIn both, each asked first", {timeout: 120000}, async () => {
  await open([posting(), reading()], async (page, served) => {
    assert.match(await card(page).locator(".integration-health").innerText(), /^cloud access enabled · checked /);
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    const rows = card(page).locator(".linkedin-part");
    assert.match(await rows.nth(1).innerText(), /Reading\s*Your posts and who engaged · cloud with browser backup\s*Tin for Chrome · your own session · checked/);
    assert.match(await card(page).locator(".integration-control-footer").innerText(), /Reading and sending use your own LinkedIn; each teammate turns on theirs\.\s*Settings\s*Disconnect LinkedIn\s*Done/);

    const asked = [];
    page.on("dialog", dialog => {asked.push(dialog.message()); dialog.accept();});
    await card(page).getByRole("button", {name: "Turn off"}).click();
    await page.locator(".toast", {hasText: "LinkedIn reading turned off."}).waitFor();
    assert.deepEqual(served.removed, ["network.linkedin"]);
    assert.match(asked[0], /^Turn off reading your LinkedIn in Example Co\? Tin deletes your saved LinkedIn session here\. Posting stays connected\.$/);

    served.integrations.splice(1, 1, reading());
    await page.reload();
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    await card(page).getByRole("button", {name: "Disconnect LinkedIn"}).click();
    await page.locator(".toast", {hasText: "LinkedIn disconnected."}).waitFor();
    assert.deepEqual(served.removed, ["network.linkedin", "social.linkedin", "network.linkedin"]);
    assert.match(asked[1], /Disconnect LinkedIn from Example Co\? Tin stops posting as Avery Example, Example Co for everyone in this project\. Tin deletes your saved LinkedIn session here\./);
    assert.equal(await card(page).locator(".integration-state-mark.is-available").count(), 1);
  });
});

test("a teammate sees the pages they can post as, not another member's profile", {timeout: 120000}, async () => {
  const theirs = posting();
  theirs.configuration.accounts = [
    {name: "Avery Example", kind: "person", active: true, can_post: false},
    {name: "Example Co", kind: "organization", active: true, can_post: true},
  ];
  await open([theirs, reading()], async (page, served) => {
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    assert.match(await card(page).locator(".linkedin-part").nth(0).innerText(), /Posting\s*Example Co · approved posts only/);
    // Manage still lists the profile, as only its owner can post as it.
    await card(page).getByRole("button", {name: "Manage"}).click();
    assert.match(await card(page).locator(".linkedin-part-manage").innerText(), /profile Avery Example · only Avery posts as it\s*page Example Co/);
    // Disconnecting stops posting for everyone, so it names every account.
    const asked = [];
    page.on("dialog", dialog => {asked.push(dialog.message()); dialog.dismiss();});
    await card(page).getByRole("button", {name: "Disconnect LinkedIn"}).click();
    assert.match(asked[0], /Tin stops posting as Avery Example, Example Co for everyone in this project\./);
    assert.deepEqual(served.removed, []);
  });
});

test("a declined confirmation removes nothing", {timeout: 120000}, async () => {
  await open([posting(), reading()], async (page, served) => {
    page.on("dialog", dialog => dialog.dismiss());
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    await card(page).getByRole("button", {name: "Disconnect LinkedIn"}).click();
    await card(page).getByRole("button", {name: "Turn off"}).click();
    assert.deepEqual(served.removed, []);
  });
});

test("only the offered part shows, and posting that needs you counts once under Needs you", {timeout: 120000}, async () => {
  await open([posting(true, "needs_attention")], async page => {
    assert.equal(await card(page).locator(".integration-key").innerText(), "posting");
    assert.deepEqual(await counts(page), ["All 2", "Connected 0", "Needs you 1", "Available 1"]);
    assert.equal(await card(page).locator(".integration-health").innerText(), "posting needs reconnecting");
    await card(page).getByRole("button", {name: "Configure", exact: true}).click();
    assert.equal(await card(page).locator(".linkedin-part").count(), 1);
    assert.match(await card(page).locator(".linkedin-part").innerText(), /LinkedIn needs reconnecting\. No connected account can post\./);
    assert.equal(await card(page).getByRole("button", {name: "Reconnect"}).getAttribute("data-integration-connect"), "social.linkedin");
    assert.equal(await card(page).locator(".integration-control-footer").innerText().then(text => text.includes("Reading and sending")), false);
  });
});

test("only reading offered: Connect goes straight to Tin for Chrome's setup", {timeout: 120000}, async () => {
  await open([reading(false)], async page => {
    assert.equal(await card(page).locator(".integration-key").innerText(), "reading · sending");
    assert.equal(await card(page).getByRole("button", {name: "Connect", exact: true}).getAttribute("data-integration-connect"), "network.linkedin");
  });
});
