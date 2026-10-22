// Packaged dashboard with synthetic APIs. No live credentials or projects.
// Tin Analytics: the card in Integrations (Paper CARD-1 to CARD-4) and the Analytics page (REP-V2,
// V2b, V2c, V2e), from the contract's example reports in tests/fixtures/agent_traffic.
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp } from "./app-fixture.js";

const fixture = name => JSON.parse(fs.readFileSync(new URL(`../tests/fixtures/agent_traffic/${name}`, import.meta.url), "utf8"));
const KEY = "analytics.agent_traffic";
const SCRIPT = '<script async src="https://edge.tin.computer/sdk/v1.js" data-site="st-8k3m9q2x7v4c"></script>';
const recent = () => new Date(Date.now() - 3600000).toISOString();

function card(state, extra = {}) {
  const setUp = ["waiting", "live", "quiet", "needs_attention"].includes(state);
  const view = setUp ? {
    state, address: "https://www.example.com", site_id: "st-8k3m9q2x7v4c", linked: false,
    counted_hosts: ["example.com", "*.example.com"], address_source: "setup", script: SCRIPT,
    csp: {"script-src": ["https://edge.tin.computer"], "connect-src": ["https://edge.tin.computer"]},
    pages_today: state === "live" ? 37 : 0, first_page_at: state === "waiting" ? null : "2026-10-02T09:31:10Z",
    last_page_at: state === "waiting" ? null : recent(), hosts_seen: state === "live" ? [{host: "www.example.com", pages_7d: 212}, {host: "blog.example.com", pages_7d: 9}] : [],
    requests_last_at: null, reporter_connected: false, moved_hosts: [], checked_at: recent(), stale: false,
  } : {state, address: "https://www.example.com"};
  Object.assign(view, extra);
  return {
    key: KEY, name: "Tin Analytics", badge: "AN", description: "See who visits your site.",
    access_label: "Visits to your site · no sign-in", capabilities: ["status.read", "reports.read"],
    unlocks: ["the Analytics page", "visits from AI assistants"], configured: state !== "unavailable",
    connection_id: setUp ? "analytics-1" : null, status: state === "needs_attention" ? "needs_attention" : setUp ? "connected" : "available",
    configuration: {}, analytics: view,
  };
}

const comparison = report => report.previous && {
  start: report.previous.start, end: report.previous.end, until: report.previous.until,
  changes: {
    visits: {now: report.totals.visits, before: report.previous.visits, percent: Math.round((report.totals.visits - report.previous.visits) * 100 / report.previous.visits)},
    assistant_visits: {now: report.totals.assistant_visits, before: report.previous.assistant_visits},
  },
};
const pageOf = (view, report, cardView) => ({card: cardView.analytics, view, tz: "Europe/Istanbul", report, comparison: report && comparison(report), fetched_at: recent(), stale: false, date: report?.end});

async function open(options, url, run) {
  const served = await serveApp({decisionCount: 0, ...options});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, served.base, {url, theme: "dark"});
    await run(page, served);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    served.server.close();
  }
}

const analyticsCard = page => page.locator(".analytics-card");

test("card: set up from Start here's address, then waiting, and the rail shows Analytics", {timeout: 120000}, async () => {
  const integrations = [card("not_set_up")];
  const writes = [];
  const analytics = {writes, pages: {}, answer: () => ({body: card("waiting")})};
  await open({integrations, analytics}, "/integrations", async page => {
    await page.locator(".integration-list").waitFor();
    assert.equal(await page.locator('.nav-item[data-view="analytics"]').isHidden(), true);
    assert.equal(await analyticsCard(page).locator(".integration-name").innerText(), "Tin Analytics");
    assert.equal(await analyticsCard(page).locator(".integration-primary").innerText(), "www.example.com");
    assert.equal(await analyticsCard(page).locator(".integration-unlocks").innerText(), "would unlock the Analytics page · visits from AI assistants");
    await analyticsCard(page).getByRole("button", {name: "Set up", exact: true}).click();
    await analyticsCard(page).getByText(/waiting for the first visit · checked \d\d:\d\d/).waitFor();
    assert.deepEqual(writes, [{action: "setup", body: {}}]);
    assert.equal(await analyticsCard(page).locator(".analytics-mark").getAttribute("class"), "analytics-mark is-waiting");
    assert.equal(await page.locator('.nav-item[data-view="analytics"]').isVisible(), true);
  });
});

test("card: a project without Start here types one address; staging's limit says so", {timeout: 120000}, async () => {
  const integrations = [card("not_set_up", {address: null})];
  const writes = [];
  let refuse = true;
  const analytics = {writes, pages: {}, answer: () => refuse
    ? {status: 409, body: {detail: {message: "Staging already has 20 analytics sites. Remove one, then set up again.", code: "staging_limit"}}}
    : {body: card("waiting")}};
  await open({integrations, analytics}, "/integrations", async page => {
    await page.locator(".integration-list").waitFor();
    assert.equal(await analyticsCard(page).locator(".integration-primary").innerText(), "no address yet");
    assert.equal(await analyticsCard(page).getByRole("button", {name: "Set up"}).count(), 1);
    await analyticsCard(page).getByLabel("Website").fill("sheepdogs.io");
    await analyticsCard(page).getByRole("button", {name: "Set up"}).click();
    await analyticsCard(page).locator(".analytics-prompt").waitFor();
    assert.equal(await analyticsCard(page).locator(".analytics-prompt").innerText(), "Staging already has 20 analytics sites. Remove one, then set up again.");
    assert.equal(await analyticsCard(page).locator(".analytics-health").innerText(), "couldn't set up on staging");
    assert.equal(await analyticsCard(page).getByLabel("Website").inputValue(), "sheepdogs.io");
    refuse = false;
    await analyticsCard(page).getByRole("button", {name: "Set up"}).click();
    await analyticsCard(page).getByText(/waiting for the first visit/).waitFor();
    assert.deepEqual(writes.map(item => item.body), [{url: "sheepdogs.io"}, {url: "sheepdogs.io"}]);
    assert.equal(await analyticsCard(page).locator(".analytics-prompt").count(), 0);
  });
});

test("card: live with the reporter, Configure, count a moved host, stop measuring", {timeout: 120000}, async () => {
  const live = card("live", {reporter_connected: true, requests_last_at: recent(), moved_hosts: [{host: "newname.com", pages_7d: 31}]});
  const integrations = [live];
  const writes = [];
  const analytics = {writes, pages: {}, answer: (action, body) => ({body: card("live", {address: `https://${body.host}`})})};
  await open({integrations, analytics}, "/integrations", async (page, served) => {
    await page.locator(".integration-list").waitFor();
    const health = analyticsCard(page).locator(".analytics-health");
    assert.equal(await health.locator("span").innerText(), "37 pages today");
    assert.equal(await health.locator("small").innerText(), "newname.com isn't counted");
    await analyticsCard(page).getByRole("button", {name: "Configure"}).click();
    const rows = analyticsCard(page).locator(".linkedin-part");
    assert.match(await rows.nth(0).innerText(), /Address\s*The script also ran on newname\.com, which isn't counted: 31 pages in the last 7 days\.\s*counting example\.com and every subdomain now · the script line stays the same\s*Count newname\.com instead/);
    assert.match(await rows.nth(1).innerText(), /Script\s*On the site since oct 2 · last page \d\d:\d\d\s*site st-8k3m9q2x7v4c · last 7 days: www\.example\.com 212 pages, blog\.example\.com 9\s*See the line/);
    assert.match(await rows.nth(2).innerText(), /Reporter\s*Every page request, including crawlers that don't run scripts\s*last request \d\d:\d\d/);
    await analyticsCard(page).getByRole("button", {name: "See the line"}).click();
    assert.equal(await analyticsCard(page).locator(".analytics-line code").innerText(), SCRIPT);
    assert.match(await analyticsCard(page).locator(".integration-control-footer").innerText(), /Visits are recorded on Tin's analytics service\. Stopping deletes this site's numbers within a day\.\s*Open Analytics\s*Stop measuring\s*Done/);
    // Stop measuring confirms in the card's foot; only the confirmation is filled red.
    await analyticsCard(page).getByRole("button", {name: "Stop measuring"}).click();
    const confirm = analyticsCard(page).locator(".analytics-confirm");
    assert.match(await confirm.innerText(), /Stop measuring example\.com\?\s*The script turns itself off on the next page load, and the site's numbers are deleted within a day\.\s*Keep measuring\s*Stop and delete/);
    const fill = await confirm.getByRole("button", {name: "Stop and delete"}).evaluate(button => getComputedStyle(button).backgroundColor);
    assert.notEqual(fill, "rgba(0, 0, 0, 0)");
    await confirm.getByRole("button", {name: "Keep measuring"}).click();
    assert.equal(await analyticsCard(page).locator(".analytics-confirm").count(), 0);
    // Counting the moved host is one press.
    await analyticsCard(page).getByRole("button", {name: "Count newname.com instead"}).click();
    await page.locator(".toast").getByText("Now counting newname.com and every subdomain. The script line on your site stays the same.").waitFor();
    assert.deepEqual(writes, [{action: "address", body: {host: "newname.com"}}]);
    await analyticsCard(page).getByRole("button", {name: "Stop measuring"}).click();
    integrations[0] = card("not_set_up", {removal: {state: "removing", requested_at: new Date().toISOString(), deleted_by: new Date(Date.now() + 86400000).toISOString()}});
    await analyticsCard(page).getByRole("button", {name: "Stop and delete"}).click();
    await analyticsCard(page).getByText(/^stopped \w+ \d+ · numbers deleted by \w+ \d+, \d\d:\d\d$/).waitFor();
    assert.deepEqual(served.removed, [KEY]);
    assert.equal(await analyticsCard(page).getByRole("button", {name: "Set up"}).count(), 1);
  });
});

test("card: needs attention, quiet, stopping and not set up on this deployment", {timeout: 120000}, async () => {
  const integrations = [card("needs_attention")];
  await open({integrations}, "/integrations", async page => {
    await page.locator(".integration-list").waitFor();
    assert.match(await analyticsCard(page).getAttribute("class"), /is-needs/);
    assert.equal(await analyticsCard(page).locator(".analytics-health").innerText(), "Tin can no longer read this site's numbers");
    assert.equal(await analyticsCard(page).getByRole("button", {name: "Set up again"}).count(), 1);
    assert.equal(await page.locator('[data-integration-filter="needs_attention"]').innerText(), "Needs you 1");
  });
  for (const [view, text, action] of [
    [card("quiet"), "No visits recorded in 7 days. Check that the script is still on the site.", "Configure"],
    [card("not_set_up", {removal: {state: "pending", requested_at: recent(), deleted_by: null}}), "stopping · Tin keeps asking until the deletion is confirmed", null],
    [card("unavailable"), "Analytics isn't set up on this deployment", null],
  ]) {
    await open({integrations: [view]}, "/integrations", async page => {
      await page.locator(".integration-list").waitFor();
      assert.equal(await analyticsCard(page).locator(".analytics-health").innerText(), text);
      assert.equal(await analyticsCard(page).locator(".integration-card-row button").count(), action ? 1 : 0);
    });
  }
});

test("page: today so far, each number against the day before, every page request", {timeout: 120000}, async () => {
  const live = card("live", {reporter_connected: true, requests_last_at: "2026-10-21T07:58:00Z", moved_hosts: [{host: "newname.com", pages_7d: 31}]});
  const today = fixture("report_day.json");
  Object.assign(today, {complete: false, start: "2026-10-21", end: "2026-10-21", built_at: "2026-10-21T08:02:00Z"});
  today.previous = {...today.previous, start: "2026-10-20", end: "2026-10-20", until: "2026-10-20T08:02:00Z"};
  today.needs_you = [{code: "uncounted_hosts", text: "The script ran on newname.com, which isn't counted: 31 pages in the last 7 days."}];
  today.top_pages.push({path: "/careers", visits: null, page_views: null});
  // An operator ID added after Tin's list stays as it is.
  today.kinds.automated_browser.named.push({operator: "newcorp", visits: 1, evidence: "network"});
  today.requests.ai_pages = [
    {path: "/about", loads: 1, by_operator: {anthropic: 1}},
    {path: "/docs/start", loads: 19, by_operator: {openai: 12, anthropic: 7}},
    {path: "/", loads: 9, by_operator: {openai: 6, anthropic: 3}},
    {path: "/pricing", loads: 5, by_operator: {openai: 5}},
    {path: "/blog/a", loads: 3, by_operator: {openai: 2, anthropic: 1}},
  ];
  const week = fixture("report_week.json");
  Object.assign(week, {complete: false});
  const pages = {today: pageOf("today", today, live), week: pageOf("week", week, live)};
  const writes = [];
  const analytics = {writes, pages, answer: () => ({body: card("live", {address: "https://newname.com"})})};
  await open({integrations: [live], analytics}, "/analytics", async page => {
    await page.locator(".analytics-tiles").waitFor();
    assert.equal(await page.locator('.nav-item[data-view="analytics"]').getAttribute("class"), "nav-item is-active");
    assert.deepEqual(await page.locator(".analytics-sections .workflow-section").allInnerTexts(), ["Today", "Yesterday", "Last 7 days"]);
    assert.equal(await page.locator(".analytics-meta").innerText(), "script on · reporter on · istanbul time · updated 11:02");
    assert.equal(await page.locator(".analytics-heading h2").first().innerText(), "Wednesday so far");
    assert.equal(await page.locator(".analytics-heading > span").first().innerText(), "oct 21 · 00:00–11:02 · against tuesday at the same hour");
    assert.equal(await page.locator(".analytics-summary p").innerText(), today.summary);
    const tiles = page.locator(".analytics-tile");
    assert.equal(await tiles.count(), 4);
    assert.deepEqual(await tiles.evaluateAll(items => items.map(item => item.innerText.split("\n").filter(Boolean))), [
      ["Visits", "48", "+17% on tuesday"],
      ["Page views", "91", "1.9 a visit"],
      ["Arrived from AI assistants", "6", "up from 4"],
      ["Automated browsers", "3", "OpenAI 2 · signed · newcorp 1 · from its network"],
    ]);
    assert.equal(await page.locator(".analytics-legend").innerText(), "no automation seen 43\nautomated browsers 3\ndeclared crawlers 2");
    assert.equal(await page.locator(".analytics-bars .analytics-bar").count(), 24);
    assert.equal(await page.locator(".analytics-bars .analytics-bar.is-future").count() > 0, true);
    assert.match(await page.locator(".analytics-detector").innerText(), /Of the 43 with no automation seen, the detector says\s*human 38 · not human 2 · unknown 2 · not scored yet 1\s*provisional · still being measured/);
    // A null count, which the service doesn't send, is a dash.
    const careers = page.locator(".analytics-row", {hasText: "/careers"});
    assert.deepEqual(await careers.locator(".analytics-num").allInnerTexts(), ["–", "–", ""]);
    assert.match(await page.locator(".analytics-row", {hasText: "chatgpt.com"}).innerText(), /chatgpt\.com\s*AI assistant\s*4/);
    assert.deepEqual(await page.locator(".analytics-class").allInnerTexts(), ["412\npage loads", "61\nverified bots", "88\nnamed bots, unconfirmed", "39\nother bots", "224\nbrowser-like · script didn't run on 173"]);
    assert.match(await page.locator(".analytics-row:not(.is-head)", {hasText: "OpenAI"}).innerText(), /OpenAI\s*fetching for someone\s*AI\s*22\s*30/);
    const aiPages = page.locator(".analytics-table").nth(3);
    assert.deepEqual(await aiPages.locator(".analytics-row.is-head span").allInnerTexts(), ["Page", "OpenAI", "Anthropic"]);
    // The three pages loaded most, then the rest of each company's loads (52 and 39 in all).
    assert.deepEqual(await aiPages.locator(".analytics-row:not(.is-head)").evaluateAll(rows => rows.map(row => row.innerText.split("\n").filter(Boolean))), [
      ["/docs/start", "12", "7"], ["/", "6", "3"], ["/pricing", "5", "0"], ["Other pages", "29", "29"],
    ]);
    assert.equal(await aiPages.locator(".analytics-card-head code").innerText(), "91 loads");
    assert.match(await page.locator(".analytics-notes").innerText(), /A visit ends after 30 minutes without activity\.$/);
    // Counting the moved host from the needs-you bar.
    await page.getByRole("button", {name: "Count newname.com instead"}).click();
    await page.getByText("Now counting newname.com and its subdomains. The script line on your site stays the same.").waitFor();
    assert.deepEqual(writes, [{action: "address", body: {host: "newname.com"}}]);
    // The last 7 days: a bar per day, the last one so far.
    await page.getByRole("tab", {name: "Last 7 days"}).click();
    await page.locator(".analytics-days").waitFor();
    assert.equal(await page.locator(".analytics-day").count(), 7);
    assert.equal(await page.locator(".analytics-day-label span").last().innerText(), "tue 20 so far");
    assert.equal(await page.locator(".analytics-tile").first().locator("code").innerText(), "+9% on the week before");
    assert.match(page.url(), /tab=week/);
  });
});

test("page: waiting for the first visit has no tabs and shows the line", {timeout: 120000}, async () => {
  const waiting = card("waiting");
  await open({integrations: [waiting], analytics: {pages: {today: pageOf("today", null, waiting)}, answer: () => ({})}}, "/analytics", async page => {
    await page.locator(".analytics-install").waitFor();
    assert.equal(await page.locator(".analytics-sections").count(), 0);
    assert.match(await page.locator(".analytics-meta").innerText(), /^no visit yet · istanbul time · checked \d\d:\d\d$/);
    assert.equal(await page.locator(".analytics-heading h2").innerText(), "Waiting for the first visit");
    assert.equal(await page.locator(".analytics-script").innerText(), SCRIPT);
    assert.match(await page.locator(".analytics-install").innerText(), /If you set a CSP\s*Add Tin's analytics address to script-src and connect-src\.\s*script-src https:\/\/edge\.tin\.computer · connect-src https:\/\/edge\.tin\.computer/);
    assert.match(await page.locator(".analytics-install").innerText(), /What's counted\s*example\.com and every subdomain, such as www\.example\.com/);
  });
});

test("page: without the reporter there is no request section", {timeout: 120000}, async () => {
  const live = card("live");
  const yesterday = fixture("report_day.json");
  yesterday.requests = null;
  yesterday.came_from = [];
  yesterday.regions = [{name: "America", visits: 304}, {name: "Europe", visits: 247}, {name: "Asia", visits: 120}, {name: "Australia", visits: 22}, {name: "Africa", visits: 15}, {name: "Pacific", visits: 4}, {name: "UTC", visits: 3}];
  yesterday.notes = ["The script can't see crawlers and assistants that don't run scripts.", ...yesterday.notes.slice(1)];
  await open({integrations: [live], analytics: {pages: {today: pageOf("today", yesterday, live)}, answer: () => ({})}}, "/analytics", async page => {
    await page.locator(".analytics-tiles").waitFor();
    assert.equal(await page.locator(".analytics-classes").count(), 0);
    assert.equal(await page.getByText("Every page request").count(), 0);
    assert.equal(await page.locator(".analytics-meta").innerText().then(text => text.includes("reporter")), false);
    assert.match(await page.locator(".analytics-notes").innerText(), /^The script can't see crawlers/);
    assert.equal(await page.locator(".analytics-needs").innerText(), "Nothing needs you today");
    const cameFrom = page.locator(".analytics-table", {hasText: "Came from"});
    assert.deepEqual(await cameFrom.locator(".analytics-row:not(.is-head)").allInnerTexts(), [
      "No visits followed a link from another site",
      "Regions, from browser time zones\nAmerica 304 · Europe 247 · Asia 120 · Australia 22 · Africa 15 · Pacific 4 · UTC 3",
    ]);
    const label = cameFrom.locator(".is-regions .is-label");
    assert.equal(await label.evaluate(node => node.scrollWidth <= node.clientWidth), true);
    // Seven regions stay inside the table, wrapping when they must.
    assert.equal(await cameFrom.evaluate(table => {
      const edge = table.getBoundingClientRect().right;
      const regions = table.querySelector(".analytics-regions");
      return regions.getBoundingClientRect().right <= edge && regions.scrollWidth <= regions.clientWidth;
    }), true);
  });
});

test("card: a linked site's Stop keeps its history", {timeout: 120000}, async () => {
  const linked = card("live", {
    address: "https://tin.computer", site_id: "organic-tin", linked: true,
    counted_hosts: ["tin.computer", "*.tin.computer"], address_source: "entered",
  });
  const integrations = [linked];
  await open({integrations, analytics: {pages: {}, answer: () => ({})}}, "/integrations", async (page, served) => {
    await page.locator(".integration-list").waitFor();
    await analyticsCard(page).getByRole("button", {name: "Configure"}).click();
    assert.match(await analyticsCard(page).locator(".integration-control-footer").innerText(), /This site was measured before Tin, so stopping keeps its history\./);
    await analyticsCard(page).getByRole("button", {name: "Stop measuring"}).click();
    const confirm = analyticsCard(page).locator(".analytics-confirm");
    assert.equal(await confirm.locator("strong").innerText(), "Stop showing Tin Analytics for this project?");
    assert.equal(await confirm.locator("p").innerText(), "Tin stops reading this site's numbers and deletes its own copies. The site's history stays, because the site was measured before Tin. If you set it up again, the numbers come back.");
    assert.equal(await confirm.getByRole("button", {name: "Stop and delete"}).count(), 0);
    // Stop deletes nothing here, so it is outlined, not filled.
    const stop = confirm.getByRole("button", {name: "Stop", exact: true});
    assert.equal(await stop.evaluate(button => getComputedStyle(button).backgroundColor), "rgba(0, 0, 0, 0)");
    integrations[0] = card("not_set_up", {removal: {state: "unlinked", requested_at: new Date().toISOString(), deleted_by: null}});
    await stop.click();
    await analyticsCard(page).getByText(/^stopped \w+ \d+ · history kept$/).waitFor();
    assert.deepEqual(served.removed, [KEY]);
  });
});
