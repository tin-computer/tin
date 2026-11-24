// Packaged dashboard with synthetic APIs. No live credentials or projects.
// People (Paper PEOPLE-A1, C1, C1b, E1–E3, N1, N1b): a grid of everyone the project keeps,
// segments, the reason behind a score, a person beside the grid, a member's corrections and
// notes, and Forget confirmed in place.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp, syntheticPeople, visitorPeople, visitorPerson } from "./app-fixture.js";

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
    // Without visitors the tab opens on All people; Hot shows once anyone is hot.
    assert.deepEqual(await page.locator(".people-sections .workflow-section").allTextContents(), ["Hot 1", "All people 5", "ICP fit 3", "Not connected yet 3"]);
    assert.equal(await page.getByRole("tab", {name: "All people 5"}).getAttribute("aria-selected"), "true");
    // A score made under an older ICP file reads "old"; a person not scored shows a dash.
    assert.equal(await page.locator('td[data-fit="p-sofia"]').textContent(), "old3");
    assert.equal(await page.locator('td[data-fit="p-hannah"]').textContent(), "—");
    assert.equal(await page.locator(".people-pager code").textContent(), "1–5 of 5");

    await page.locator('td[data-fit="p-sofia"]').hover();
    const hover = page.locator(".people-hover.is-visible");
    await hover.waitFor();
    assert.match(await hover.innerText(), /no marketing lead[\s\S]*3 · Good fit · ICP 1 · oct 15[\s\S]*Rated before strategy\/ICP\.md or Tin’s rating changed\. The next run rates again\./);

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

// Where the grid would be left narrower than its compact layout beside the panel, the panel floats
// over it instead: opening someone moves nothing under the cursor (filmed in Films · People).
test("on a narrower screen a person opens over the grid, which doesn't move", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    for (const [width, beside] of [[720, false], [1024, false], [1440, true]]) {
      const {page, context, errors} = await openApp(browser, base, {url: "/people", viewport: {width, height: 900}});
      await page.locator(".people-table").waitFor();
      const layout = () => page.evaluate(() => ({
        grid: document.querySelector(".people-grid").getBoundingClientRect().toJSON(),
        row: document.querySelector('tr[data-person="p-maya"]').getBoundingClientRect().top,
      }));
      const before = await layout();
      await page.getByRole("row", {name: "Maya Lindqvist"}).click();
      await page.locator("#person-panel").getByRole("link", {name: "Open on LinkedIn"}).waitFor();
      const after = await layout();
      assert.equal(after.row, before.row, `at ${width}`);
      if (beside) assert.ok(after.grid.width < before.grid.width, `at ${width} the grid narrows beside the panel`);
      else assert.deepEqual(after.grid, before.grid, `at ${width} the panel floats over the grid`);
      assert.deepEqual(errors, []);
      await context.close();
    }
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
    // Last touch says who was behind it: you, or the teammate by name (TEAM-A).
    const touch = (name) => page.getByRole("row", {name}).locator("td.is-touch").innerText();
    assert.equal(await touch("Maya Lindqvist"), "oct 13 · reacted to you");
    assert.equal(await touch("Daniel Okafor"), "oct 16 · invited by Mira");
    assert.equal(await touch("Hannah Kim"), "oct 17 · viewed Mira");
    await page.getByRole("row", {name: "Maya Lindqvist"}).click();
    const panel = page.locator("#person-panel");
    await panel.getByRole("link", {name: "Open on LinkedIn"}).waitFor();
    assert.equal((await panel.boundingBox()).width, 420);
    assert.equal(await page.locator(".people-table th.is-headline").isVisible(), false);
    assert.equal(await page.locator(".people-table th.is-touch").isVisible(), false);
    assert.equal(new URL(page.url()).searchParams.get("person"), "p-maya");
    assert.match(await panel.locator(".person-section").first().innerText(), /Fields\s*5[\s\S]*ICP fit\s*4 · Strong fit\s*engagers · oct 15[\s\S]*Intent\s*🔥 hot[\s\S]*Customer\s*No/);
    await panel.getByText("Reacted to your post with Insightful").waitFor();
    assert.equal(await panel.getByText("Viewed your profile").count(), 1);
    // The timeline names the teammate too (TEAM-C).
    assert.equal(await panel.getByText("Reacted to Mira’s post with Like").count(), 1);
    assert.equal(await panel.getByText("Mira sent a connection request").count(), 1);
    assert.equal(await panel.locator("a.person-event").first().getAttribute("target"), "_blank");
    assert.equal(await panel.locator("footer code").textContent(), "added oct 15 by engagers");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);

    // A reload opens the same person; Escape in the panel closes it.
    await page.reload();
    await page.locator("#person-panel h2").getByText("Maya Lindqvist").waitFor();
    await page.locator("#person-panel").focus();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#person-panel").count(), 0);
    // It slides out the way it came, inert while it goes, then is gone.
    const leaving = page.locator("aside.person-panel.is-leaving");
    if (await leaving.count()) assert.equal(await leaving.evaluate(panel => panel.inert), true);
    await page.locator("aside.person-panel").waitFor({state: "detached"});
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
    assert.match(await panel.locator("footer").innerText(), /Forget Lea Fischer\?\s*Tin deletes this person’s fields, notes and timeline in Example Co and won’t add them again, even when they react to a later post or view a profile\. The list in Files drops them at the next run\./);
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

test("a member rates someone from a menu, and can give Tin's score back", {timeout: 120000}, async () => {
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
    await panel.getByRole("button", {name: "1 · Not a fit"}).click();
    const menu = panel.getByRole("listbox", {name: "ICP fit"});
    assert.deepEqual(await menu.getByRole("option").allInnerTexts(), [
      "4 · Strong fit", "3 · Good fit", "2 · Weak fit", "1 · Not a fit\nTin",
    ]);
    assert.match(await menu.innerText(), /Your rating replaces Tin’s, and later runs keep it\./);
    // Escape closes the menu, not the person.
    await page.keyboard.press("Escape");
    assert.equal(await menu.count(), 0);
    assert.equal(await panel.count(), 1);

    await panel.getByRole("button", {name: "1 · Not a fit"}).click();
    await menu.getByRole("option", {name: "3 · Good fit"}).click();
    await panel.getByText("Set by you. Tin rated 1 · Not a fit: Runs a small B2B team with no marketing lead.").waitFor();
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /ICP fit\s*3 · Good fit\s*you · /);
    // The grid shows who set it, and the person now counts as a fit.
    assert.equal(await page.locator('td[data-fit="p-lea"]').textContent(), "you3");
    assert.equal(await page.getByRole("tab", {name: /^ICP fit/}).textContent(), "ICP fit 4");

    await panel.getByRole("button", {name: "Use Tin’s score"}).click();
    await page.locator('td[data-fit="p-lea"]', {hasText: /^1$/}).waitFor();
    // Back to Tin's rating; the fixture dates it by the edit, so the day depends on the clock.
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /ICP fit\s*1 · Not a fit\s*engagers · [a-z]{3} \d{1,2}/);
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
    assert.equal(await page.locator(".people-table th.is-notes").count(), 0);
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
    assert.match(await panel.locator(".person-field").nth(1).innerText(), /old · 4 · Strong fit[\s\S]*Your note came after this rating\. The next run reads it and rates again\./);
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
    assert.match(await notes.nth(1).innerText(), /Met at a founder dinner in Berlin\.\s*Mira · oct 14/);
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
    // Anyone who visited makes Visitors the tab People opens on.
    assert.deepEqual(
      await page.locator(".people-sections .workflow-section").allTextContents(),
      ["Visitors 1", "Hot 1", "All people 6", "ICP fit 3", "Not connected yet 3"],
    );
    assert.equal(await page.getByRole("tab", {name: "Visitors 1"}).getAttribute("aria-selected"), "true");
    const row = page.getByRole("row", {name: "Ana Souza"});
    // Known by her email alone: no LinkedIn, so her name is plain text.
    assert.equal(await row.locator("td.is-name a").count(), 0);
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

test("Visitors opens first, with each person's company and intent, sorted hot first", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true, people: [...syntheticPeople(), ...visitorPeople()]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    const names = () => page.locator(".people-table tbody td.is-name").allInnerTexts();
    assert.deepEqual(
      await page.locator(".people-sections .workflow-section").allTextContents(),
      ["Visitors 2", "Hot 2", "All people 9", "ICP fit 5", "Not connected yet 3", "From ads 1"],
    );
    // RB2B's visitor and the person found for their company's visit; imported connections wait in All people.
    assert.equal(await page.getByRole("tab", {name: "Visitors 2"}).getAttribute("aria-selected"), "true");
    assert.deepEqual(await names(), ["Rosa Alvarez", "Omar Haddad"]);
    // LinkedIn has no column of its own: the name is the way there.
    const headers = await page.locator(".people-table thead th").allInnerTexts();
    assert.deepEqual(headers.map(text => text.trim()), ["Name", "Company", "Headline", "ICP", "Fit", "Intent", "Came from", "Last touch"]);
    assert.equal(await page.locator(".people-table th.is-linkedin").count(), 0);
    const rosa = page.getByRole("row", {name: "Rosa Alvarez"});
    assert.equal(await rosa.locator("td.is-company").innerText(), "Valley Bank");
    assert.equal(await rosa.locator("td.is-intent").innerText(), "🔥 hot");
    // Intent has its own column; the fit cell holds only the number.
    assert.equal(await rosa.locator("td.is-fit").innerText(), "4");
    assert.equal(await page.getByRole("row", {name: "Omar Haddad"}).locator("td.is-touch").innerText(), "oct 19");
    assert.equal(await rosa.locator("td.is-touch").innerText(), "oct 20");

    await page.getByRole("tab", {name: "Hot 2"}).click();
    assert.deepEqual(await names(), ["Maya Lindqvist", "Rosa Alvarez"]);
    await page.getByRole("tab", {name: "All people 9"}).click();
    await page.getByRole("button", {name: "Intent"}).click();
    // Hot, warm, cold, then no intent; names break ties.
    assert.deepEqual((await page.locator(".people-table tbody td.is-intent").allInnerTexts()).slice(0, 5), ["🔥 hot", "🔥 hot", "🌤 warm", "🌤 warm", "🧊 cold"]);
    assert.deepEqual((await names()).slice(0, 2), ["Maya Lindqvist", "Rosa Alvarez"]);
    assert.equal(await page.locator(".people-table th.is-intent").getAttribute("aria-sort"), "descending");
    await page.getByRole("button", {name: "Company"}).click();
    // Companies A to Z, people without one last.
    assert.deepEqual(await page.locator(".people-table tbody td.is-company").allInnerTexts(), ["Fieldline", "Lumen", "Northwind Freight", "Patchbay", "Paywise", "Quanta Labs", "Valley Bank", "—", "—"]);
    // Search reads the company too.
    await page.getByLabel("Search people").fill("northwind");
    assert.deepEqual(await names(), ["Omar Haddad"]);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

// The name is a link to LinkedIn, in a new tab; the row around it, a separate stop for the keyboard,
// opens the person beside the grid.
test("the name opens LinkedIn in a new tab, and the rest of the row opens the person", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    const row = page.getByRole("row", {name: "Maya Lindqvist"});
    const link = row.getByRole("link", {name: "Maya Lindqvist on LinkedIn (opens in a new tab)"});
    assert.equal(await link.getAttribute("href"), "https://www.linkedin.com/in/p-maya");
    assert.equal(await link.getAttribute("target"), "_blank");
    assert.equal(await link.getAttribute("rel"), "noopener noreferrer");

    // Clicking the name opens LinkedIn and leaves the panel shut.
    const [tab] = await Promise.all([context.waitForEvent("page"), link.click()]);
    await tab.close();
    assert.equal(await page.locator("#person-panel").count(), 0);
    // Anywhere else on the row, even the name's cell past the name, opens the person.
    await row.locator("td.is-name").click({position: {x: 200, y: 20}});
    await page.locator("#person-panel h2").getByText("Maya Lindqvist").waitFor();
    await page.keyboard.press("Escape");
    await page.locator("aside.person-panel").waitFor({state: "detached"});

    // The keyboard: the row is one stop (Enter opens the person), the name the next.
    await row.focus();
    await page.keyboard.press("Enter");
    await page.locator("#person-panel h2").getByText("Maya Lindqvist").waitFor();
    await page.keyboard.press("Escape");
    await page.locator("aside.person-panel").waitFor({state: "detached"});
    assert.equal(await page.evaluate(() => document.activeElement.dataset.person), "p-maya");
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-label")), "Maya Lindqvist on LinkedIn (opens in a new tab)");
    const [again] = await Promise.all([context.waitForEvent("page"), page.keyboard.press("Enter")]);
    await again.close();
    assert.equal(await page.locator("#person-panel").count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

test("the panel names the way in, where they work, and a visit only their company made", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true, people: [...syntheticPeople(), ...visitorPeople()]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    const panel = page.locator("#person-panel");
    const field = label => panel.locator(".person-field", {has: page.locator(".person-field-line > span", {hasText: new RegExp(`^${label}$`)})});

    await page.getByRole("row", {name: "Rosa Alvarez"}).locator("td.is-headline").click();
    await field("Way in").waitFor();
    // The same 🤝 line Slack posts, from People.
    assert.match(await field("Way in").innerText(), /^Way in\s*🤝 Emre knows Mira Chen \(Treasury\) at Valley Bank$/);
    assert.match(await field("Intent").innerText(), /Intent\s*🔥 hot/);
    // Her headline already says her role and company, so only where she lives is added.
    assert.equal(await panel.locator(".person-panel-work").innerText(), "Austin, Texas");
    assert.equal(await panel.locator(".person-panel-found").count(), 0);

    // Beside the panel the grid is compact; another row opens from its ICP cell.
    await page.getByRole("row", {name: "Omar Haddad"}).locator("td.is-icp").click();
    await panel.locator(".person-panel-found").waitFor();
    assert.equal(await panel.locator(".person-panel-found").innerText(), "Found for a company visit");
    assert.equal(await panel.locator(".person-panel-work").innerText(), "Chicago, Illinois");
    // No path, no row.
    assert.equal(await field("Way in").count(), 0);

    // Someone with no path to them has no Way in row.
    await page.getByRole("tab", {name: "All people 9"}).click();
    await page.getByRole("row", {name: "Maya Lindqvist"}).locator("td.is-icp").click();
    await panel.getByText("Reacted to your post with Insightful").waitFor();
    assert.equal(await field("Way in").count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

// A connection of a teammate reads with their first name, or "a teammate"; "you" is only the
// viewer's own.
test("a teammate's connection is named, not called yours", {timeout: 120000}, async () => {
  const {server, base} = await serveApp({engagers: true, people: [...syntheticPeople(), ...visitorPeople()]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    await page.getByRole("tab", {name: "All people 9"}).click();
    const touch = name => page.getByRole("row", {name}).locator("td.is-touch").innerText();
    assert.equal(await touch("Inês Costa"), "oct 2 · connected to Emre");
    assert.equal(await touch("Tom Becker"), "sep 28 · connected to a teammate");
    assert.equal(await touch("Maya Lindqvist"), "oct 13 · reacted to you");
    await page.getByRole("row", {name: "Inês Costa"}).locator("td.is-headline").click();
    const panel = page.locator("#person-panel");
    await panel.getByText("🤝 Emre is already connected").waitFor();
    assert.equal(await panel.getByText("Connected with Emre on LinkedIn").count(), 1);
    assert.equal(await panel.getByText(/with you/).count(), 0);
    await page.getByRole("row", {name: "Tom Becker"}).locator("td.is-icp").click();
    await panel.getByText("🤝 Your team is already connected").waitFor();
    assert.equal(await panel.getByText("Connected with a teammate on LinkedIn").count(), 1);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});

// Ten thousand imported connections: the grid shows fifty at a time and stays quick to sort and search.
test("a list of ten thousand people stays paged and quick", {timeout: 120000}, async () => {
  const people = syntheticPeople();
  for (let index = 0; index < 10500; index++) {
    people.push({...structuredClone(people[4]), id: `p-conn-${index}`, name: `Connection ${String(index).padStart(5, "0")}`, company: `Company ${index % 700}`, fields: {}, last_touch: {kind: "connected", at: "2026-11-20T10:00:00Z", who: "you"}});
  }
  const {server, base} = await serveApp({engagers: true, people: [...people, ...visitorPeople()]});
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors} = await openApp(browser, base, {url: "/people"});
    await page.locator(".people-table").waitFor();
    // Visitors still open first, ahead of the connections.
    assert.deepEqual(await page.locator(".people-table tbody td.is-name").allInnerTexts(), ["Rosa Alvarez", "Omar Haddad"]);
    await page.getByRole("tab", {name: /^All people/}).click();
    assert.equal(await page.getByRole("tab", {name: /^All people/}).textContent(), "All people 10,509");
    assert.equal(await page.locator(".people-table tbody tr[data-person]").count(), 50);
    assert.equal(await page.locator(".people-pager code").textContent(), "1–50 of 10,509");
    const started = Date.now();
    await page.getByRole("button", {name: "Company"}).click();
    await page.getByRole("button", {name: "Next page"}).click();
    await page.getByLabel("Search people").fill("Connection 10499");
    assert.deepEqual(await page.locator(".people-table tbody td.is-name").allInnerTexts(), ["Connection 10499"]);
    assert.ok(Date.now() - started < 5000, `${Date.now() - started}ms to sort, page and search`);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
});
