import assert from "node:assert/strict";
import test from "node:test";
import {chromium} from "playwright";

for (const theme of ["light", "dark"]) test(`project author selection keeps confirmed identities: ${theme}`, async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1100,height:850}});
    await page.route("http://localhost/authors", route => route.fulfill({contentType:"text/html",body:"<!doctype html><html></html>"}));
    await page.goto("http://localhost/authors");
    await page.setContent(`<html data-theme="${theme}"><body><main class="workspace"><form class="system-template-card is-open"><div class="system-template-setup-body"><section class="x-workflow-fields" id="fields"></section><section><p>On demand</p></section></div></form></main></body></html>`);
    await page.addStyleTag({path:"src/tin_lite/static/app.css"});
    await page.addStyleTag({path:"src/tin_lite/static/x-posts.css"});
    await page.addScriptTag({path:"src/tin_lite/static/project-file-input.js"});
    await page.addScriptTag({path:"src/tin_lite/static/project-author-input.js"});
    await page.evaluate(() => {
      fields.innerHTML = `<div class="system-setting"><strong>Author</strong>${TinProjectAuthorInput.field("input:author_id", "", "Author", "author")}</div><div class="system-setting"><strong>Audience</strong>${TinProjectFileInput.field("input:icp", "context/AUDIENCE.md", "Audience", "audience", true)}</div><label class="system-setting">What do you want to share?<textarea class="workflow-inline-input" id="direction">Our latest project update</textarea></label>`;
      window.current = true; window.fileReads = 0; window.changes = 0;
      document.querySelector("form").addEventListener("change", () => changes++);
      window.authorRows = [{id:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",display_name:"Alex",email:"one@example.test",is_me:true,member_clerk_user_id:"user_one",version:1,guide_path:"style/alex-one.md"},{id:"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",display_name:"Alex",email:"two@example.test",member_clerk_user_id:"user_two",version:1,guide_path:"style/alex-two.md"}];
      window.services = {projectId:"project",isCurrent:()=>current,api:async (url, options) => {
        if (options?.method === "POST") return authorRows.find(a=>a.member_clerk_user_id === url.split("/").at(-1));
        if (options?.method === "PUT") {
          window.updated = JSON.parse(options.body);
          const row = authorRows.find(a=>a.id === url.split("/").at(-1));
          Object.assign(row, {guide_path:updated.selected_guide,version:row.version+1}); return row;
        }
        if (url.endsWith("/authors?members=true")) return {authors:authorRows,default_author_id:"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"};
        fileReads++; return {revision:"a".repeat(40),files:[{path:"context/AUDIENCE.md"},{path:"style/alex-one.md"},{path:"style/existing.md"}]};
      }};
      TinProjectFileInput.bind(document.querySelector("form"), services);
      TinProjectAuthorInput.bind(document.querySelector("form"), services);
    });
    await page.getByRole("link",{name:"Open writing guide"}).waitFor();
    assert.equal(await page.getByLabel("Author",{exact:true}).inputValue(), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    assert.equal(await page.evaluate(()=>fileReads), 1, "author and file fields share one listing");
    const options = await page.getByLabel("Author",{exact:true}).locator("option").allTextContents();
    assert.ok(options.includes("Alex · one@example.test (you)"));
    assert.ok(options.includes("Alex · two@example.test"));
    await page.getByLabel("Author",{exact:true}).selectOption("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb");
    await page.getByText("Choose an existing guide, or use Learn my writing style to prepare one.",{exact:true}).waitFor();
    assert.equal(await page.getByRole("link",{name:"Open writing guide"}).count(), 0);
    assert.equal(await page.locator("#direction").inputValue(), "Our latest project update");
    await page.getByLabel("Author",{exact:true}).selectOption("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    for (const width of [1100,390]) {
      await page.setViewportSize({width,height:850});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth), true);
      if (process.env.TIN_AUTHOR_SCREENSHOTS) await page.screenshot({path:`${process.env.TIN_AUTHOR_SCREENSHOTS}/author-input-${theme}-${width}.png`,fullPage:true});
    }
    await page.evaluate(() => {
      fields.insertAdjacentHTML("beforeend", `<div id="saved">${TinProjectAuthorInput.field("input:saved", "unavailable", "Saved author", "saved-author")}</div>`);
      TinProjectAuthorInput.bind(document.querySelector("#saved"), services);
    });
    await page.getByLabel("Saved author",{exact:true}).locator("option[value=unavailable]").waitFor({state:"attached"});
    assert.equal(await page.getByLabel("Saved author",{exact:true}).inputValue(), "unavailable", "never replace an unavailable saved author with the member's default");
    assert.ok(await page.evaluate(()=>changes>=3));
    const first = page.locator("[data-project-author-input]").first();
    assert.equal(await first.getByText("Add an author",{exact:true}).count(), 0);
    assert.equal(await first.getByLabel("This is me",{exact:true}).count(), 0);
    await first.getByRole("button",{name:"Change writing guide",exact:true}).click();
    await first.getByLabel("Existing writing guide").selectOption("style/existing.md");
    await first.getByRole("button",{name:"Use this guide",exact:true}).click();
    await page.waitForFunction(()=>window.updated?.selected_guide === "style/existing.md");
    assert.deepEqual(await page.evaluate(()=>updated), {display_name:"Alex",expected_version:1,selected_guide:"style/existing.md"});
    assert.equal(await page.locator("#direction").inputValue(), "Our latest project update");
  } finally {await browser.close();}
});
