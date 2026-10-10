import assert from "node:assert/strict";
import fs from "node:fs/promises";
import test from "node:test";
import { chromium } from "playwright";

for (const theme of ["light", "dark"]) test(`style source preparation: ${theme}, mobile, upload and replay`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 1000}});
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("http://localhost/style-test", r => r.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/style-test");
    await page.setContent(`<html data-theme="${theme}"><body><main class="workspace"><form class="system-template-card is-open workflow-config-form"><div class="system-template-setup-body"><section id="samples"></section></div></form></main></body></html>`);
    await page.addStyleTag({content: await fs.readFile("src/tin_lite/static/app.css", "utf8")});
    await page.addStyleTag({content: await fs.readFile("src/tin_lite/static/x-posts.css", "utf8")});
    await page.addScriptTag({path: "src/tin_lite/static/project-author-input.js"});
    await page.addScriptTag({path: "src/tin_lite/static/style-capture.js"});
    await page.evaluate(() => {
      document.querySelector("#samples").innerHTML = window.TinStyleCapture.fields();
      window.writes = []; window.changed = false; window.failOnce = true;
      window.services = {
        projectId: "project", openAgent() {}, assertCurrent() {if (window.changed) throw new Error("Project changed.");},
        fetch: async () => ({text: async () => "Existing project prose."}),
        api: async (path, options) => {
          if (path.includes("/preview?")) return {label: "sample.txt", text: "Uploaded writing.", warnings: []};
          if (!options) return {revision: "a".repeat(40), files: [{path: "notes/article.md"}]};
          window.writes.push(JSON.parse(options.body));
          if (window.failOnce) {window.failOnce = false; throw new Error("Lost response; retry.");}
          return {revision: "b".repeat(40)};
        },
      };
      window.TinStyleCapture.bind(document.querySelector("form"), services);
      Object.defineProperty(navigator.clipboard, "writeText", {value: async (text) => {window.copiedPrompt = text;}});
    });
    assert.equal(await page.locator("[data-style-purpose]").isVisible(), false);
    assert.equal(await page.locator("[data-style-agent]").isVisible(), true);
    await page.locator("[data-style-agent]").click();
    const prompt = await page.evaluate(() => window.copiedPrompt);
    assert.match(prompt, /blog posts I've written as links or files/);
    assert.match(prompt, /one short invitation/);
    assert.match(prompt, /not extra questions to ask if my writing samples are sufficient/);
    assert.match(prompt, /get_workflow's preparation/);
    assert.match(prompt, /Obsidian.*Codex, Claude Code or other harness sessions/);
    assert.match(prompt, /scoped read permission/);
    assert.match(prompt, /Do not silently substitute/);
    assert.equal(await page.evaluate(() => writes.length), 0);
    await page.locator("[data-style-samples] > summary").click();
    await page.locator("[data-style-kind]").selectOption("conversation");
    await page.locator("[data-style-text]").fill('<script>alert("no")</script> Start with the useful question.');
    await page.locator("[data-style-add]").click();
    assert.equal(await page.locator("[data-style-list] script").count(), 0);
    await page.locator("[data-style-upload]").setInputFiles({name: "sample.txt", mimeType: "text/plain", buffer: Buffer.from("Uploaded writing.")});
    await page.waitForFunction(() => document.querySelectorAll("[data-style-list] details").length === 2);
    await page.locator("[data-style-files]").click();
    await page.locator("[data-style-add-file]").click();
    await page.waitForFunction(() => document.querySelectorAll("[data-style-list] details").length === 3);
    await page.locator("[data-style-list] details").first().locator("summary").click();
    for (const width of [1100, 390]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_STYLE_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_STYLE_SCREENSHOTS}/style-${theme}-${width}.png`, fullPage: true});
    }
    const firstError = await page.evaluate(async () => {try {await TinStyleCapture.prepare(document.querySelector("form"), services);} catch (e) {return e.message;}});
    assert.match(firstError, /Lost response/);
    await page.evaluate(() => TinStyleCapture.prepare(document.querySelector("form"), services));
    assert.deepEqual(await page.evaluate(() => writes[0]), await page.evaluate(() => writes[1]));
    const packet = await page.evaluate(() => JSON.parse(writes[0].changes[0].content.split("```json\n")[1].split("\n```")[0]));
    assert.deepEqual(packet.samples.map(s => s.id), ["s1", "s2", "s3"]);
    assert.equal(packet.samples[0].kind, "conversation");
    assert.match(packet.samples[2].origin, /notes\/article.md@a{40}/);
    assert.match(await page.locator('[name="input:source_path"]').inputValue(), /^style\/sources\//);
    await page.evaluate(() => {window.changed = true;});
    await page.locator("[data-style-add]").click();
    assert.match(await page.locator("[data-style-message]").innerText(), /Project changed/);
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});

for (const theme of ["light", "dark"]) test(`author capture keeps samples separate: ${theme}`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 1000}});
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("http://localhost/author-test", r => r.fulfill({body: "<!doctype html><html><body></body></html>", contentType: "text/html"}));
    await page.goto("http://localhost/author-test");
    await page.setContent(`<html data-theme="${theme}"><body><main class="workspace"><form class="system-template-card is-open workflow-config-form"><div class="system-template-setup-body"><section id="samples"></section></div></form></main></body></html>`);
    await page.addStyleTag({content: await fs.readFile("src/tin_lite/static/app.css", "utf8")});
    await page.addStyleTag({content: await fs.readFile("src/tin_lite/static/x-posts.css", "utf8")});
    await page.addScriptTag({path: "src/tin_lite/static/project-author-input.js"});
    await page.addScriptTag({path: "src/tin_lite/static/style-capture.js"});
    await page.evaluate(() => {
      document.querySelector("#samples").innerHTML = TinStyleCapture.fields({authors: true});
      window.writes = [];
      window.authors = [{id: "author-one", display_name: "Alex", member_clerk_user_id: "user_one", email: "one@example.test"}, {id: "author-two", display_name: "Alex", member_clerk_user_id: "user_two", email: "two@example.test", is_me: true}];
      window.services = {
        projectId: "project", openAgent() {}, assertCurrent() {},
        api: async (path, options) => {
          if (path.endsWith("/authors?members=true")) return {authors: window.authors, default_author_id: "author-two"};
          if (path.includes("/authors/member/")) {
            if (window.failSelection) throw new Error("Member temporarily unavailable");
            return window.authors.find(a=>a.member_clerk_user_id === path.split("/").pop());
          }
          if (!options) return {revision: "a".repeat(40), files: []};
          window.writes.push(JSON.parse(options.body));
          return {revision: "b".repeat(40)};
        },
      };
      TinStyleCapture.bind(document.querySelector("form"), services);
      Object.defineProperty(navigator.clipboard, "writeText", {value: async (text) => {window.copiedPrompt = text;}});
    });
    await page.waitForFunction(() => !document.querySelector("[data-style-author]").disabled);
    assert.equal(await page.locator("[data-style-author]").inputValue(), "author-two");
    await page.locator("[data-style-agent]").click();
    assert.match(await page.evaluate(() => copiedPrompt), /author_id author-two/);
    await page.locator("[data-style-samples] > summary").click();
    await page.locator("[data-style-preferences]").fill("Use examples from my work.");
    await page.locator("[data-style-text]").fill("Unsaved personal passage.");
    await page.locator("[data-style-author]").selectOption("author-one");
    assert.equal(await page.locator("[data-style-preferences]").inputValue(), "");
    assert.equal(await page.locator("[data-style-text]").inputValue(), "");
    await page.locator("[data-style-preferences]").fill("A different author's preferences.");
    await page.locator("[data-style-author]").selectOption("author-two");
    assert.equal(await page.locator("[data-style-text]").inputValue(), "Unsaved personal passage.");
    assert.equal(await page.locator("[data-style-preferences]").inputValue(), "Use examples from my work.");
    await page.locator("[data-style-add]").click();
    await page.evaluate(() => TinStyleCapture.prepare(document.querySelector("form"), services));
    const packet = await page.evaluate(() => writes[0].changes[0].content);
    assert.match(packet, /Use examples from my work/);
    assert.doesNotMatch(packet, /different author's/);
    assert.match(await page.locator("[data-style-author-note]").innerText(), /Other authors' guides stay unchanged/);
    await page.locator("[data-style-author]").selectOption("");
    assert.equal(await page.locator('[name="input:author_id"]').count(), 0);
    assert.match(await page.locator("[data-style-author-note]").innerText(), /shared project/);
    assert.equal(await page.getByText("Add an author", {exact:true}).count(), 0);
    await page.evaluate(() => {window.failSelection = true;});
    await page.locator("[data-style-author]").selectOption("author-one");
    await page.getByText("Member temporarily unavailable", {exact:true}).waitFor();
    assert.equal(await page.evaluate(async () => {
      try {await TinStyleCapture.prepare(document.querySelector("form"), services); return "unexpected";}
      catch (e) {return e.message;}
    }), "Choose an available author before continuing.");
    assert.equal(await page.locator("[data-style-text]").inputValue(), "");
    await page.evaluate(() => {window.failSelection = false;});
    await page.locator("[data-style-author]").selectOption("author-two");
    await page.waitForFunction(() => document.querySelector("[data-style-author]").dataset.ready === "true");

    for (const width of [1100, 390]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_STYLE_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_STYLE_SCREENSHOTS}/author-${theme}-${width}.png`, fullPage: true});
    }
    assert.deepEqual(errors, []);
  } finally {await browser.close();}
});
