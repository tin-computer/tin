// Packaged dashboard with synthetic APIs. No live credentials, projects or LinkedIn.
// Connection requests (Paper INVITE-A to INVITE-C, REQ-A): a batch waits in Decisions, approving
// queues the ticked people for Tin to send one at a time, people ticked in People are queued the
// same way, and sending is turned on, paused and resumed on the LinkedIn card.
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
const person = (n, name, fit) => ({
  request_id: `q-${n}`, record_id: `p-${n}`, name, headline: `Founder, Company ${n}`,
  icp: "ICP 1: founders who need growth done", fit, engaged: {kind: "reacted", at: "2026-10-13T08:00:00Z"},
});

function requests({on = false, held = false} = {}) {
  const inAnHour = new Date(Date.now() + 3600000).toISOString();
  return {
    offered: true,
    reading: true,
    sending: {on, paused: held, held, paused_reason: held ? "challenge" : null, paused_until: null,
      daily_cap: 15, weekly_cap: 60, start_minute: 540, end_minute: 1020, timezone: "Europe/Berlin"},
    pause_text: held ? "LinkedIn asked to verify your account" : null,
    consent: {version: 1, text: "Send the connection requests I approve from Tin’s servers, with my LinkedIn session: one at a time, a few each weekday.", risk: "LinkedIn’s terms don’t allow automated requests."},
    week: {sent: 9, accepted: 3, queued: 5, pending: 6, next_send: inAnHour},
    next_open: on ? inAnHour : null,
    batches: [{
      id: "batch-1", created_at: new Date(Date.now() - 7200000).toISOString(),
      first_send: inAnHour, last_send: new Date(Date.now() + 3 * 86400000).toISOString(),
      people: [person(1, "Maya Lindqvist", 0.84), person(2, "Daniel Okafor", 0.78), person(3, "Amara Osei", 0.66)],
    }],
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

test("a batch waits first in Decisions, and approving sends only the ticked people", {timeout: 120000}, async () => {
  await open({url: "/decisions", state: requests({on: true})}, async (page, served) => {
    const card = page.locator(".requests-card");
    await card.waitFor();
    assert.deepEqual(await page.locator(".decision-group-title").allInnerTexts(), ["Connection requests\n1", "Reviews\n1"]);
    assert.match(await page.locator(".decision-list-item.is-active").innerText(), /Send requests to 3 people/);
    assert.match(await card.innerText(), /3 people who reacted to your posts fit your ICP and aren’t connected to you yet\./);
    assert.match(await card.innerText(), /one at a time on its own schedule, never together/);
    // The window is the project's time zone, named when it isn't the viewer's.
    assert.match(await card.innerText(), /weekdays 9:00–17:00 Europe\/Berlin, at most 15 a day/);
    // An hour from now is tomorrow late in the day.
    assert.match(await card.innerText(), /The first goes out around \d+:\d\d (today|tomorrow), the last on/);
    assert.equal(await card.locator(".request-row").count(), 3);
    await card.getByRole("checkbox").nth(2).uncheck();
    const approve = card.getByRole("button", {name: "Approve 2 requests"});
    await approve.click();
    await page.locator(".toast", {hasText: "Approved. Tin sends the 2 one at a time"}).waitFor();
    const write = served.requestWrites.find(item => item.path.endsWith("/approve"));
    assert.deepEqual(write.body.people, ["p-1", "p-2"]);
    assert.match(write.body.request_id, /^[0-9a-f-]{36}$/);
    await page.locator(".requests-card").waitFor({state: "detached"});
  });
});

test("with sending off, the batch asks you to turn it on first", {timeout: 120000}, async () => {
  await open({url: "/decisions", state: requests({on: false})}, async page => {
    const card = page.locator(".requests-card");
    await card.waitFor();
    assert.equal(await card.getByRole("button", {name: "Approve 3 requests"}).isDisabled(), true);
    assert.match(await card.innerText(), /Turn on sending on the LinkedIn card first\./);
    await card.getByRole("button", {name: "Open the LinkedIn card"}).click();
    await page.locator(".linkedin-card.is-expanded .requests-sending").waitFor();
    assert.equal(new URL(page.url()).pathname, "/integrations");
  });
});

test("turning sending back on starts with the consent box ticked, then shows the week", {timeout: 120000}, async () => {
  await open({url: "/integrations", state: requests({on: false})}, async (page, served) => {
    await page.locator(".linkedin-card").getByRole("button", {name: "Configure"}).click();
    const row = page.locator(".requests-sending");
    await row.waitFor();
    assert.match(await row.innerText(), /Sending\s*Send connection requests you approve, one at a time/);
    await row.getByRole("switch").click();
    // Members agreed to sending when they joined Tin: the box starts ticked, and unticking it
    // still holds the button.
    const turnOn = row.getByRole("button", {name: "Turn on sending"});
    assert.equal(await row.getByRole("checkbox").isChecked(), true);
    assert.equal(await turnOn.isDisabled(), false);
    assert.match(await row.innerText(), /LinkedIn’s terms don’t allow automated requests\./);
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
