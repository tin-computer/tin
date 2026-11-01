// Packaged dashboard with synthetic APIs. No live credentials, projects or LinkedIn.
// Connection requests (Paper INVITE-A to INVITE-C, REQ-A): Tin asks the people who fit on its own,
// so nothing waits in Decisions; people ticked in People are queued at once, and sending is
// turned on, paused and resumed on the LinkedIn card.
import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { openApp, serveApp, syntheticPeople } from "./app-fixture.js";

const soon = () => new Date(Date.now() + 86400000).toISOString();
const reading = {
  key: "network.linkedin", name: "LinkedIn", badge: "in", description: "Read posts and who engaged.", unlocks: ["People who engaged"],
  configured: true, connection_id: "reading-1", status: "connected",
  configuration: {actor: {name: "Avery Example"}, collection_permission: {version: 1, mode: "cloud_preferred"},
    session_state: "available", session_expires_at: soon(), session_verified_at: new Date(Date.now() - 3600000).toISOString()},
  connected_at: "2026-10-14T09:00:00Z",
};
function requests({on = false, held = false} = {}) {
  const inAnHour = new Date(Date.now() + 3600000).toISOString();
  return {
    offered: true,
    reading: true,
    sending: {on, paused: held, held, paused_reason: held ? "challenge" : null, paused_until: null,
      daily_cap: 15, weekly_cap: 60, start_minute: 540, end_minute: 1020, timezone: "Europe/Berlin"},
    pause_text: held ? "LinkedIn asked to verify your account" : null,
    consent: {version: 1, text: "Send connection requests from my LinkedIn to people who fit my ICP and engaged with my posts or viewed my profile, and to anyone I choose. A few each weekday, stopping at LinkedIn’s first warning. I can turn it off anytime."},
    week: {sent: 9, accepted: 3, queued: 5, pending: 6, next_send: inAnHour},
    next_open: on ? inAnHour : null,
  };
}

async function open({url, state, integrations = [reading], people}, run) {
  const served = await serveApp({decisionCount: 1, integrations, requests: state, ...(people ? {people, engagers: true} : {})});
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

test("connection requests don't wait in Decisions", {timeout: 120000}, async () => {
  await open({url: "/decisions", state: requests({on: true})}, async page => {
    await page.locator(".decision-list-item").first().waitFor();
    assert.deepEqual(await page.locator(".decision-group-title").allInnerTexts(), ["Reviews\n1"]);
    assert.equal(await page.locator(".requests-card").count(), 0);
  });
});

test("turning sending back on starts with the consent box ticked, then shows the week", {timeout: 120000}, async () => {
  await open({url: "/integrations", state: requests({on: false})}, async (page, served) => {
    await page.locator(".linkedin-card").getByRole("button", {name: "Configure"}).click();
    const row = page.locator(".requests-sending");
    await row.waitFor();
    assert.match(await row.innerText(), /Sending\s*Send connection requests, one at a time/);
    await row.getByRole("switch").click();
    // Members agreed to sending when they joined Tin: the box starts ticked, and unticking it
    // still holds the button.
    const turnOn = row.getByRole("button", {name: "Turn on sending"});
    assert.equal(await row.getByRole("checkbox").isChecked(), true);
    assert.equal(await turnOn.isDisabled(), false);
    assert.match(await row.innerText(), /to people who fit my ICP and engaged with my posts or viewed my profile, and to anyone I choose/);
    assert.doesNotMatch(await row.innerText(), /LinkedIn’s terms/);
    await row.getByRole("checkbox").uncheck();
    assert.equal(await turnOn.isDisabled(), true);
    await row.getByRole("checkbox").check();
    await row.getByRole("button", {name: "Turn on sending"}).click();
    await page.locator(".toast", {hasText: "Sending is on."}).waitFor();
    assert.deepEqual(served.requestWrites.at(-1), {method: "PUT", path: "/sending", body: {enabled: true, consent_version: 1}});
    const on = page.locator(".requests-sending");
    assert.match(await on.innerText(), /15 a day · 60 a week · weekdays 9:00–17:00 Europe\/Berlin · 15\+ min apart/);
    assert.match(await page.locator(".linkedin-card").innerText(), /This week\s*9 sent · 3 accepted · 5 queued · 6 pending · next send/);
    assert.match(await page.locator(".linkedin-card .integration-health").innerText(), /sending on · 9 sent this week · 3 accepted/);

    await on.getByRole("button", {name: "Change limits"}).click();
    await on.getByLabel("A day").fill("10");
    await on.getByRole("button", {name: "Save limits"}).click();
    await page.locator(".toast", {hasText: "Limits saved."}).waitFor();
    assert.equal(served.requestWrites.at(-1).body.limits.daily_cap, 10);
  });
});

test("with reading in this browser only, sending waits for cloud reading", {timeout: 120000}, async () => {
  await open({url: "/integrations", state: {...requests({on: false}), reading: false, browser_only: true}}, async page => {
    await page.locator(".linkedin-card").getByRole("button", {name: "Configure"}).click();
    const row = page.locator(".requests-sending");
    assert.match(await row.innerText(), /one at a time · turns on with cloud reading/);
    assert.equal(await row.getByRole("switch").isDisabled(), true);
  });
});

test("a warning from LinkedIn pauses sending until you resume it", {timeout: 120000}, async () => {
  await open({url: "/integrations", state: requests({on: true, held: true})}, async (page, served) => {
    await page.locator(".linkedin-card").waitFor();
    assert.deepEqual(await page.locator("[data-integration-filter]").allInnerTexts(), ["All 2", "Connected 0", "Needs you 1", "Available 1"]);
    assert.match(await page.locator(".linkedin-card .integration-health").innerText(), /sending paused/);
    await page.locator(".linkedin-card").getByRole("button", {name: "Configure"}).click();
    const row = page.locator(".requests-sending");
    assert.match(await row.innerText(), /Sending paused: LinkedIn asked to verify your account\. Open LinkedIn in Chrome/);
    await row.getByRole("button", {name: "Resume sending"}).click();
    await page.locator(".toast", {hasText: "Sending resumed."}).waitFor();
    assert.equal(served.requestWrites.at(-1).path, "/sending/resume");
    assert.equal(await page.locator(".requests-sending .linkedin-part-needs").count(), 0);
  });
});

test("People shows who was invited, in the column and as a segment", {timeout: 120000}, async () => {
  const people = syntheticPeople();
  const maya = people.find(item => item.id === "p-maya");
  maya.fields.linkedin_relationship = {...maya.fields.linkedin_relationship, value: "pending", by: "requests"};
  maya.invited_at = "2026-10-16T09:40:00Z";
  await open({url: "/people", state: requests({on: true}), people}, async page => {
    await page.locator(".people-table").waitFor();
    assert.match(await page.getByRole("tab", {name: /^Invited/}).innerText(), /^Invited\s+1$/);
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-linkedin').innerText(), "Invited oct 16");
    await page.getByRole("tab", {name: /^Invited/}).click();
    assert.deepEqual(await page.locator(".people-table tbody td.is-name").allInnerTexts(), ["Maya Lindqvist"]);
  });
});

test("people ticked in People are queued at once, best fit first", {timeout: 120000}, async () => {
  await open({url: "/people", state: requests({on: true}), people: syntheticPeople()}, async (page, served) => {
    await page.locator(".people-table").waitFor();
    // Only people you can ask get a box: not your connections.
    assert.equal(await page.locator('tr[data-person="p-maya"] td.is-pick input').count(), 0);
    assert.equal(await page.locator(".people-picked").count(), 0);
    await page.locator('tr[data-person="p-lea"] td.is-pick input').check();
    await page.locator('tr[data-person="p-daniel"] td.is-pick input').check();
    // Ticking doesn't open the person.
    assert.equal(await page.locator("#person-panel").count(), 0);
    const bar = page.locator(".people-picked");
    assert.match(await bar.innerText(), /^2 selected\s+Clear/);
    assert.match(await bar.innerText(), /One at a time from your LinkedIn, weekdays 9:00–17:00 Europe\/Berlin · the first around \d+:\d\d (today|tomorrow)/);
    assert.equal(await page.locator('tr[data-person="p-daniel"]').getAttribute("class"), " is-picked");
    await bar.getByRole("button", {name: "Send 2 requests"}).click();
    await page.locator(".toast", {hasText: /Queued\. Tin sends the 2 one at a time from your LinkedIn, the first around \d+:\d\d/}).waitFor();
    const write = served.requestWrites.find(item => item.path === "/send");
    assert.deepEqual(write.body.people, ["p-daniel", "p-lea"]);
    assert.match(write.body.request_id, /^[0-9a-f-]{36}$/);
    assert.equal(await page.locator(".people-picked").count(), 0);
    assert.equal(await page.locator('tr[data-person="p-daniel"] td.is-linkedin').innerText(), "Queued");
    assert.equal(await page.locator('tr[data-person="p-daniel"] td.is-pick input').count(), 0);
  });
});

// Paper PJ-A: the bar lies over the header row, beside the select-all box, at the row's height, so
// ticking someone moves nothing under the cursor; header and bar stay pinned on a long list.
test("ticking someone turns the header row into the bar, and nothing under it moves", {timeout: 120000}, async () => {
  const people = syntheticPeople();
  for (let index = 0; index < 30; index++) people.push({...structuredClone(people[3]), id: `p-more-${index}`, name: `Person ${index}`});
  await open({url: "/people", state: requests({on: true}), people}, async page => {
    await page.locator(".people-table").waitFor();
    const grid = () => page.evaluate(() => {
      const box = element => element && (({top, left, right, height}) => ({top, left, right, height}))(element.getBoundingClientRect());
      return {
        // The header cells are what pin; the row itself stays where it is in the table.
        head: (cells => ({top: cells[0].top, height: cells[0].height, left: cells[0].left, right: cells.at(-1).right}))([...document.querySelectorAll(".people-table thead th")].map(cell => cell.getBoundingClientRect()).filter(cell => cell.width)),
        bar: box(document.querySelector(".people-selection")),
        row: box(document.querySelector(".people-table tbody tr")),
        columns: [...document.querySelectorAll(".people-table tbody tr:first-child td")].map(cell => cell.getBoundingClientRect().width),
      };
    });
    for (const width of [1440, 720]) {
      await page.setViewportSize({width, height: 900});
      await page.evaluate(() => window.scrollTo(0, 0));
      const before = await grid();
      await page.locator('tr[data-person="p-daniel"] td.is-pick input').check();
      const after = await grid();
      assert.equal(before.bar, null);
      assert.deepEqual(after.row, before.row);
      assert.deepEqual(after.columns, before.columns);
      assert.equal(after.head.height, 34);
      assert.deepEqual([after.bar.top, after.bar.height, after.bar.left - after.head.left, after.bar.right], [after.head.top, 34, 36, after.head.right]);
      // The pacing line has room on a wide grid and steps aside on a narrow one.
      assert.equal(await page.locator(".people-selection p").isVisible(), width === 1440);
      await page.evaluate(() => window.scrollTo(0, 1200));
      const pinned = await grid();
      const top = width === 1440 ? 0 : 105;
      assert.equal(pinned.head.top, top);
      assert.equal(pinned.bar.top, top);
      await page.locator(".people-selection").getByRole("button", {name: "Clear"}).click();
    }
  });
});

test("the box above the grid ticks everyone you can ask on the page", {timeout: 120000}, async () => {
  await open({url: "/people", state: requests({on: true}), people: syntheticPeople()}, async page => {
    const all = page.locator("[data-people-pick-all]");
    await all.waitFor();
    await page.locator('tr[data-person="p-hannah"] td.is-pick input').check();
    assert.equal(await all.evaluate(box => box.indeterminate), true);
    await all.click();
    assert.match(await page.locator(".people-picked").innerText(), /^3 selected/);
    await page.locator(".people-picked").getByRole("button", {name: "Clear"}).click();
    assert.equal(await page.locator(".people-picked").count(), 0);
  });
});

test("a queued request says when it goes in the panel, and can be cancelled", {timeout: 120000}, async () => {
  await open({url: "/people", state: requests({on: true}), people: syntheticPeople()}, async (page, served) => {
    await page.locator('tr[data-person="p-daniel"] td.is-pick input').check();
    await page.locator(".people-picked").getByRole("button", {name: "Send 1 request"}).click();
    await page.locator('tr[data-person="p-daniel"] td.is-linkedin', {hasText: "Queued"}).waitFor();
    await page.getByRole("row", {name: "Daniel Okafor"}).click();
    const row = page.locator("#person-panel .person-field", {hasText: "LinkedIn"});
    assert.match(await row.innerText(), /^LinkedIn\s*Queued\s*you · [a-z]{3} \d{1,2}\s*Tin sends it from your LinkedIn around \d+:\d\d (today|tomorrow), one at a time with your other requests\. No note\.\s*Cancel request$/);
    await row.getByRole("button", {name: "Cancel request"}).click();
    await page.locator(".toast", {hasText: "Request cancelled. Tin won’t send it."}).waitFor();
    assert.deepEqual(served.requestWrites.at(-1), {method: "POST", path: "/people/p-daniel/cancel", body: {}});
    assert.match(await row.innerText(), /^LinkedIn\s*Not connected/);
    assert.equal(await page.locator('tr[data-person="p-daniel"] td.is-linkedin').innerText(), "Not connected");
    assert.equal(await page.locator('tr[data-person="p-daniel"] td.is-pick input').count(), 1);
  });
});

test("when nobody on the list can be asked, the bar's place says why", {timeout: 120000}, async () => {
  const relate = (people, value) => people.forEach(person => {
    person.fields.linkedin_relationship = {...person.fields.linkedin_relationship, value};
    person.requestable = false;
  });
  const people = syntheticPeople();
  relate(people, "connected");
  const daniel = people.find(item => item.id === "p-daniel");
  daniel.fields.linkedin_relationship.value = "pending";
  daniel.invited_at = "2026-10-15T09:40:00Z";
  await open({url: "/people", state: requests({on: true}), people}, async page => {
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator(".people-nobody").innerText(), "No one here to ask. Everyone on this list is already connected to you or invited.");
    assert.equal(await page.locator("[data-people-pick-all]").isDisabled(), true);
  });
  // A reason Tin can't name reads plainly.
  const unnamed = syntheticPeople();
  relate(unnamed, "none");
  await open({url: "/people", state: requests({on: true}), people: unnamed}, async page => {
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator(".people-nobody").innerText(), "No one on this list can be asked right now.");
  });
});

test("with sending off, the bar sends you to the LinkedIn card", {timeout: 120000}, async () => {
  await open({url: "/people", state: requests({on: false}), people: syntheticPeople()}, async page => {
    await page.locator('tr[data-person="p-daniel"] td.is-pick input').check();
    const bar = page.locator(".people-picked");
    assert.match(await bar.innerText(), /Turn on sending on the LinkedIn card first\./);
    assert.equal(await bar.getByRole("button", {name: "Send 1 request"}).isDisabled(), true);
    await bar.getByRole("button", {name: "Open the LinkedIn card"}).click();
    await page.locator(".linkedin-card.is-expanded .requests-sending").waitFor();
    assert.equal(new URL(page.url()).pathname, "/integrations");
  });
});

test("without your own LinkedIn reading, People has no boxes", {timeout: 120000}, async () => {
  const integrations = [{...reading, configured: false, connection_id: null, status: "available", configuration: {}}];
  await open({url: "/people", state: {...requests({on: false}), reading: false, browser_only: false}, integrations, people: syntheticPeople()}, async page => {
    await page.locator(".people-table").waitFor();
    assert.equal(await page.locator(".people-table .is-pick").count(), 0);
  });
});
