import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs/promises";
import {chromium} from "playwright";
import {serveApp, openApp} from "./app-fixture.js";

const text = title => ({type: "string", title, default: "", "x-tin-ui": {control: "textarea"}});
const style = {id: "x-style", key: "social.x_style", title: "Learn my X writing style", version_label: "1.0.0", description: "Learn from your own posts or samples.", definition: {executor: "social.x_style", schedule_modes: ["on_demand"], input_schema: {type: "object", properties: {
  project_id: {type: "string"}, sample_source: {type: "string", enum: ["auto", "connected", "supplied"]},
  supplied_samples: text("Your writing samples"), preferences: text("Writing preferences"), direction: text("Anything to change?"),
  source_path: {type: "string"}, account_id: {type: "string"},
}}}};
const composeDefinition = JSON.parse(await fs.readFile("workflow_packages/social.x_compose/workflow.json", "utf8")).definition;
const compose = {id: "x-compose", key: composeDefinition.key, title: composeDefinition.title, description: composeDefinition.description, version_label: "1.0.0", definition: composeDefinition};

async function setup(browser, base, connected = true) {
  const opened = await openApp(browser, base);
  opened.page.setDefaultTimeout(5000);
  const writes = [];
  await opened.page.route(url => url.pathname === "/api/workflows", route => route.fulfill({json: [style, compose]}));
  await opened.page.route("**/api/projects/project/integrations", route => route.fulfill({json: connected ? [{key: "social.x", status: "connected", external_account_label: "@example"}] : []}));
  await opened.page.route("**/api/projects/project/workflows", async route => {
    if (route.request().method() === "GET") return route.fulfill({json: []});
    const body = route.request().postDataJSON();
    writes.push(body);
    const workflow = body.workflow_id === style.id ? style : compose;
    return route.fulfill({json: {id: "saved-x", workflow_id: workflow.id, workflow_key: workflow.key, inputs: body.inputs, input_schema: workflow.definition.input_schema, status: "active", name: body.name, schedule: body.schedule, settings_revision: 1}});
  });
  await opened.page.reload();
  await opened.page.getByRole("button", {name: "Add workflows", exact: true}).click();
  return {...opened, writes};
}

for (const connected of [true, false]) test(`X voice setup uses the selected source (${connected ? "connected" : "supplied"}) without exposing IDs`, async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors, writes} = await setup(browser, base, connected);
    await page.locator('[data-configure-workflow="x-style"]').click();
    const form = page.locator('form[data-workflow-id="x-style"]');
    assert.equal(await form.locator('[name="input:account_id"]').count(), 0);
    assert.equal(await form.getByText("Every day", {exact: true}).count(), 0);
    await form.getByRole("button", {name: "My samples", exact: true}).click();
    await form.getByLabel("Your writing samples", {exact: true}).fill("An example post I wrote about a working feature.");
    await form.getByRole("button", {name: "Samples in project Files", exact: true}).click();
    await form.getByRole("option", {name: "context/positioning.md", exact: true}).click();
    await form.getByLabel("Writing preferences", {exact: true}).fill("Keep technical detail.");
    if (connected) {
      await form.getByRole("button", {name: "Connected X account", exact: true}).click();
      assert.match(await form.innerText(), /@example · Connected/);
      assert.equal(await form.locator('[name="input:supplied_samples"]').isVisible(), false);
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await form.locator("[data-save-workflow]").click();
    await page.waitForFunction(() => !document.querySelector('.system-template-card.is-open'));
    assert.equal(writes.length, 1);
    assert.equal(writes[0].schedule, null);
    assert.equal(writes[0].inputs.preferences, "Keep technical detail.");
    assert.equal(writes[0].inputs.sample_source, connected ? "connected" : "supplied");
    assert.equal(writes[0].inputs.source_path, connected ? undefined : "context/positioning.md");
    assert.equal(writes[0].inputs.supplied_samples, connected ? undefined : "An example post I wrote about a working feature.");
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});

test("X drafting setup keeps optional context together and offers manual runs only", async () => {
  const {server, base} = await serveApp();
  const browser = await chromium.launch({headless: true});
  try {
    const {page, context, errors, writes} = await setup(browser, base);
    await page.locator('[data-configure-workflow="x-compose"]').click();
    const form = page.locator('form[data-workflow-id="x-compose"]');
    await form.getByLabel("What should this post say?", {exact: true}).fill("Describe our working project Files demo.");
    await form.getByLabel("Number of posts", {exact: true}).fill("2");
    await form.locator("summary").click();
    await form.getByLabel("Existing images or video", {exact: true}).fill("demo.mp4");
    assert.match(await form.innerText(), /Manually/);
    assert.equal(await form.locator('[name="input:account_id"]').count(), 0);
    await form.locator("[data-save-workflow]").click();
    await page.waitForFunction(() => !document.querySelector('.system-template-card.is-open'));
    assert.equal(writes.length, 1);
    assert.equal(writes[0].schedule, null);
    assert.equal(writes[0].inputs.post_count, 2);
    assert.deepEqual(writes[0].inputs.asset_paths, ["demo.mp4"]);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {await browser.close(); server.close();}
});
