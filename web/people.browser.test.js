// Packaged dashboard with synthetic APIs. No live credentials or projects.
// People (Paper PEOPLE-A1, C1, C1b, E1–E3, N1, N1b): a grid of everyone the project keeps,
// segments, the reason behind a score, a person beside the grid, a member's corrections and
// notes, and Forget confirmed in place.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp, syntheticPeople, visitorPerson } from "./app-fixture.js";

const title = page => page.locator("#main h1").first().evaluate(node => {
  const style = getComputedStyle(node);
  return {family: style.fontFamily, weight: style.fontWeight, size: style.fontSize, tracking: style.letterSpacing, leading: style.lineHeight};
});

test("People shows in the menu only where a workflow that finds people is offered", {timeout: 120000}, async () => {
  for (const engagers of [true, false]) {
    const {server, base} = await serveApp({engagers});
    const browser = await chromium.launch({headless: true});
    try {
      const {page, context, errors} = await openApp(browser, base, {url: "/system"});
      await page.locator(".system-header h1").waitFor();
      assert.equal(await page.locator('.nav-item[data-view="people"]').isVisible(), engagers);
      if (engagers) {
        await page.locator('.nav-item[data-view="people"]').click();
        await page.locator(".people-table").waitFor();
        assert.equal(new URL(page.url()).pathname, "/people");
      }
      assert.deepEqual(errors, []);
      await context.close();
    } finally {
      await browser.close();
      server.close();
    }
  }
});

test("People's title is set like System's", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const system = await openApp(browser, base, {url: "/system"});
    await system.page.locator(".system-header h1").waitFor();
    const expected = await title(system.page);
    await system.context.close();
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator("#main h1").textContent(), "People");
    assert.deepEqual(await title(page), expected);
    assert.equal(expected.weight, "700");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("the grid lists people best fit first, by segment, with the reason behind a score", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator('.nav-item[data-view="people"]').getAttribute("class"), "nav-item is-active");
    const names = () => page.locator(".people-table tbody td.is-name").allTextContents();
    assert.deepEqual(await names(), ["Maya Lindqvist", "Daniel Okafor", "Sofia Marchetti", "Lea Fischer", "Hannah Kim"]);
    assert.deepEqual(await page.locator(".people-sections .workflow-section").allTextContents(), ["All people 5", "ICP fit 3", "Not connected yet 3"]);
    // A score made under an older ICP file reads "old"; a person not scored shows a dash.
    assert.equal(await page.locator('td[data-fit="p-sofia"]').textContent(), "old0.66");
    assert.equal(await page.locator('td[data-fit="p-hannah"]').textContent(), "—");
    assert.equal(await page.locator(".people-pager code").textContent(), "1–5 of 5");

    await page.locator('td[data-fit="p-sofia"]').hover();
    const hover = page.locator(".people-hover.is-visible");
    await hover.waitFor();
    assert.match(await hover.innerText(), /no marketing lead[\s\S]*0\.66 · ICP 1 · oct 15[\s\S]*Scored before strategy\/ICP\.md or Tin’s scoring changed\. The next run rescores it\./);

    await page.getByRole("tab", {name: "Not connected yet 3"}).click();
    assert.deepEqual(await names(), ["Daniel Okafor", "Lea Fischer", "Hannah Kim"]);
    await page.getByRole("tab", {name: "ICP fit 3"}).click();
    assert.deepEqual(await names(), ["Maya Lindqvist", "Daniel Okafor", "Sofia Marchetti"]);
    await page.getByRole("button", {name: "Name"}).click();
    assert.deepEqual(await names(), ["Daniel Okafor", "Maya Lindqvist", "Sofia Marchetti"]);
    await page.getByRole("tab", {name: "All people 5"}).click();
    await page.getByLabel("Search people").fill("patchbay");
    assert.deepEqual(await names(), ["Sofia Marchetti"]);
    await page.getByLabel("Search people").fill("nobody");
    assert.equal(await page.locator(".people-table tr.is-none").textContent(), "No one matches “nobody”.");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("a person opens beside a narrower grid, and the address keeps them", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator(".people-table th.is-headline").isVisible(), true);
    await page.getByRole("row", {name: "Maya Lindqvist"}).click();
    const panel = page.locator("#person-panel");
    await panel.getByRole("link", {name: "Open on LinkedIn"}).waitFor();
    assert.equal((await panel.boundingBox()).width, 420);
    assert.equal(await page.locator(".people-table th.is-headline").isVisible(), false);
    assert.equal(await page.locator(".people-table th.is-touch").isVisible(), false);
    assert.equal(new URL(page.url()).searchParams.get("person"), "p-maya");
    assert.match(await panel.locator(".person-section").first().innerText(), /Fields\s*4[\s\S]*ICP fit\s*0\.84\s*engagers · oct 15[\s\S]*Customer\s*No/);
    await panel.getByText("Reacted to your post with Insightful").waitFor();
    assert.equal(await panel.getByText("Reacted to a teammate’s post with Like").count(), 1);
    assert.equal(await panel.locator("a.person-event").first().getAttribute("target"), "_blank");
    assert.equal(await panel.locator("footer code").textContent(), "added oct 15 by engagers");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);

    // A reload opens the same person; Escape in the panel closes it.
    await page.reload();
    await page.locator("#person-panel h2").getByText("Maya Lindqvist").waitFor();
    await page.locator("#person-panel").focus();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#person-panel").count(), 0);
    assert.equal(new URL(page.url()).searchParams.get("person"), null);
    assert.equal(await page.locator(".people-table th.is-headline").isVisible(), true);

    // Leaving People closes the person.
    await page.getByRole("row", {name: "Daniel Okafor"}).click();
    await page.locator('.nav-item[data-view="files"]').click();
    assert.equal(await page.locator("#person-panel").count(), 0);
    assert.equal(new URL(page.url()).searchParams.get("person"), null);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("Forget asks in place first, then removes the person", {timeout: 120000}, async () => {
  const {server, base, people} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people", theme: "dark"});
    await page.getByRole("row", {name: "Lea Fischer"}).click();
    const panel = page.locator("#person-panel");
    await panel.getByRole("button", {name: "Forget this person"}).click();
    assert.match(await panel.locator("footer").innerText(), /Forget Lea Fischer\?\s*Tin deletes this person’s fields, notes and timeline in Example Co and won’t add them again, even when they react to a later post\. The list in Files drops them at the next run\./);
    await panel.getByRole("button", {name: "Cancel"}).click();
    assert.equal(people.length, 5);
    await panel.getByRole("button", {name: "Forget this person"}).click();
    await panel.getByRole("button", {name: "Forget", exact: true}).click();
    await page.locator("#person-panel").waitFor({state: "detached"});
    assert.equal(people.some(item => item.id === "p-lea"), false);
    assert.equal(await page.getByRole("row", {name: "Lea Fischer"}).count(), 0);
    assert.equal(await page.locator(".toast").textContent(), "Forgot Lea Fischer.");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("without people, People says how they arrive", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({people: [], engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    const empty = page.locator(".people-body .decisions-empty");
    await empty.waitFor();
    assert.match(await empty.innerText(), /No people yet\.\s*When someone reacts to your LinkedIn posts, Tin adds them here/);
    assert.equal(await page.locator(".people-view .search-field").isVisible(), false);
    await empty.getByRole("button", {name: "Open System"}).click();
    await page.locator(".system-header h1").waitFor();
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("on a phone the grid scrolls inside the page and the person fills the screen", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people", viewport: {width: 390, height: 800}});
    await page.locator(".people-table").waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.getByRole("row", {name: "Maya Lindqvist"}).click();
    const panel = await page.locator("#person-panel").boundingBox();
    assert.equal(Math.round(panel.width), 390);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("a member picks the ICP from a menu, and can give Tin's score back", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people", theme: "dark"});
    await page.locator(".people-table").waitFor();
    // The full grid names an ICP by its short label; the cell keeps the whole heading.
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-icp').innerText(), "ICP 1");
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-icp').getAttribute("title"), "ICP 1: founders who need growth done");
    await page.getByRole("row", {name: "Lea Fischer"}).click();
    const panel = page.locator("#person-panel");
    await panel.getByRole("button", {name: "None"}).click();
    const menu = panel.getByRole("listbox", {name: "ICP"});
    assert.deepEqual(await menu.getByRole("option").allInnerTexts(), [
      "ICP 1: founders who need growth done", "ICP 2: technical founders running their own marketing", "Not a fit\nTin 0.15",
    ]);
    assert.match(await menu.innerText(), /Your pick replaces Tin’s score, and later runs keep it\./);
    // Escape closes the menu, not the person.
    await page.keyboard.press("Escape");
    assert.equal(await menu.count(), 0);
    assert.equal(await panel.count(), 1);

    await panel.getByRole("button", {name: "None"}).click();
    await menu.getByRole("option", {name: "ICP 2: technical founders running their own marketing"}).click();
    await panel.getByText("Set by you. Tin scored 0.15: Runs a small B2B team with no marketing lead.").waitFor();
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /ICP fit\s*1\.00\s*you · /);
    // The grid shows who set it, and the person now counts as a fit.
    assert.equal(await page.locator('td[data-fit="p-lea"]').textContent(), "you1.00");
    assert.equal(await page.getByRole("tab", {name: /^ICP fit/}).textContent(), "ICP fit 4");

    await panel.getByRole("button", {name: "Use Tin’s score"}).click();
    await page.locator('td[data-fit="p-lea"]', {hasText: "0.15"}).waitFor();
    // Back to Tin's score; the fixture dates it by the edit, so the day depends on the clock.
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /ICP fit\s*0\.15\s*engagers · [a-z]{3} \d{1,2}/);
    assert.equal(await page.getByRole("tab", {name: /^ICP fit/}).textContent(), "ICP fit 3");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("a customer is marked in the panel and named in the grid", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.getByRole("row", {name: "Maya Lindqvist"}).click();
    const panel = page.locator("#person-panel");
    await panel.getByRole("button", {name: "No"}).click();
    await panel.getByRole("listbox", {name: "Customer"}).getByRole("option", {name: "Yes"}).click();
    await panel.getByText("Tin leaves customers out of invitations and reach lists.").waitFor();
    assert.match(await panel.locator(".person-field").last().innerText(), /Customer\s*Yes\s*you · /);
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-name').innerText(), "Maya Lindqvist\ncustomer");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("notes show in the grid and the panel, and a new one makes the score old", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people", theme: "dark"});
    await page.locator(".people-table").waitFor();
    // The grid shows the start of the newest note, and the whole note on hover.
    const cell = page.locator('td[data-note="p-daniel"]');
    assert.equal(await cell.innerText(), "Interned with us in 2024 before founding Fieldline.");
    await cell.hover();
    const hover = page.locator(".people-hover.is-visible");
    await hover.waitFor();
    assert.match(await hover.innerText(), /Interned with us in 2024 before founding Fieldline\.\nA friend of the team\.\s*you · oct 15 · latest of 2 notes/);
    // Search reads every note.
    await page.getByLabel("Search people").fill("dinner");
    assert.deepEqual(await page.locator(".people-table tbody td.is-name").allInnerTexts(), ["Daniel Okafor"]);
    await page.getByLabel("Search people").fill("");

    await page.getByRole("row", {name: "Maya Lindqvist"}).click();
    // Beside the panel, the grid drops Notes like Headline, and the ICP reads in full.
    assert.equal(await page.locator(".people-table th.is-notes").isVisible(), false);
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-icp').innerText(), "ICP 1: founders who need growth done");
    const panel = page.locator("#person-panel");
    const box = panel.getByRole("textbox", {name: "Add a note"});
    assert.equal(await box.getAttribute("placeholder"), "Add a note about Maya…");
    assert.equal(await panel.getByRole("button", {name: "Save note"}).count(), 0);
    await box.fill("Customer since September; renews in March.");
    await panel.getByRole("button", {name: "Save note"}).click();
    await panel.locator(".person-note p", {hasText: "renews in March"}).waitFor();
    assert.match(await panel.locator(".person-notes h3").innerText(), /Notes\s*1/);
    assert.equal(await box.inputValue(), "");
    // The score Tin made before the note is old until the next run reads it.
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /old 0\.84[\s\S]*Your note came after this score\. The next run reads it and rescores\./);
    assert.equal(await page.locator('td[data-note="p-maya"]').innerText(), "Customer since September; renews in March.");

    await panel.getByRole("button", {name: "Edit"}).click();
    await panel.getByRole("textbox", {name: "Edit note"}).fill("Customer since September.");
    await panel.getByRole("button", {name: "Save", exact: true}).click();
    await panel.locator(".person-note p", {hasText: "Customer since September."}).waitFor();
    assert.match(await panel.locator(".person-note-meta code").innerText(), /you · \w+ \d+ · edited/);
    await panel.getByRole("button", {name: "Delete"}).click();
    await page.locator(".toast", {hasText: "Note deleted."}).waitFor();
    assert.equal(await panel.locator(".person-note").count(), 0);

    // A teammate's note has no Edit or Delete.
    await page.getByRole("row", {name: "Daniel Okafor"}).click();
    const notes = panel.locator(".person-note");
    await notes.nth(1).waitFor();
    assert.equal(await notes.nth(0).getByRole("button", {name: "Delete"}).count(), 1);
    assert.equal(await notes.nth(1).getByRole("button", {name: "Delete"}).count(), 0);
    assert.match(await notes.nth(1).innerText(), /Met at a founder dinner in Berlin\.\s*a teammate · oct 14/);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("someone a visitor journeys run named reads by their email, their visit and its replay", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({visitors: true, people: [...syntheticPeople(), visitorPerson()]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/system"});
    await page.locator(".system-header h1").waitFor();
    // Visitor journeys finds people too, so People shows in the menu without LinkedIn.
    await page.locator('.nav-item[data-view="people"]').click();
    await page.locator(".people-table").waitFor();
    assert.deepEqual(
      await page.locator(".people-sections .workflow-section").allTextContents(),
      ["All people 6", "ICP fit 3", "Not connected yet 3", "On the site 1"],
    );
    await page.getByRole("tab", {name: "On the site 1"}).click();
    const row = page.getByRole("row", {name: "Ana Souza"});
    assert.equal(await row.locator("td.is-headline").textContent(), "Signed up from the hackathon page, picked a plan, stopped at Connect GitHub.");
    await page.getByRole("tab", {name: "All people 6"}).click();
    await page.getByLabel("Search people").fill("fieldnotes.dev");
    assert.deepEqual(await page.locator(".people-table tbody td.is-name").allTextContents(), ["Ana Souza"]);

    await row.click();
    const panel = page.locator("#person-panel");
    await panel.getByText("ana@fieldnotes.dev").waitFor();
    assert.equal(await panel.getByRole("link", {name: "Open on LinkedIn"}).count(), 0);
    assert.match(await panel.locator(".person-section").first().innerText(), /On the site\s*New user\s*visitors · oct 16[\s\S]*Stopped at \/scan\/connect/);
    const visit = panel.locator("a.person-event", {hasText: "Visited: Signed up from the hackathon page"});
    assert.equal(await visit.getAttribute("href"), "https://us.posthog.com/project/1/replay/01a1449a-9164-7200-8316-b1ea9c8c006a");
    assert.equal(await visit.getAttribute("target"), "_blank");
    assert.equal(await panel.locator(".person-event-note").textContent(), "Suggested: Email her a two-line guide to connecting GitHub.");
    assert.equal(await panel.locator("footer code").textContent(), "added oct 16 by visitors");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});
