// Real packaged dashboard in Chromium; identity and HTTP responses are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

for (const theme of ["light", "dark"]) test(`code setup and saved schedule: ${theme}`, async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id:"project", name:"Fixture project", workspace_id:"workspace", workspace_name:"Fixture", timezone:"UTC", member_count:1};
  const schema = {type:"object", properties:{minimum_cents:{type:"integer", minimum:0, title:"Minimum cents"}, notes:{type:"string",maxLength:200,title:"Saved notes",default:"Default notes","x-tin-ui":{advanced:true}}}, required:["minimum_cents"]};
  const workflow = {id:"code", key:"custom.report", title:"Order report", description:"Fixture report", version_label:"1.0", status:"active", executor:"workflow.code", allowed_actions:["start","save"], definition:{executor:"workflow.code", input_schema:schema, schedule_modes:["on_demand"]}};
  let configured = {id:"saved", project_id:"project", workflow_id:"code", workflow_key:workflow.key, workflow_title:workflow.title, name:"Weekly orders", definition_commit_sha:"a".repeat(40), inputs:{minimum_cents:1000,notes:"Remembered notes"}, input_schema:schema, status:"active", settings_revision:1, created_at:"2026-09-16T00:00:00Z", schedule:{cadence:"weekly", weekdays:["monday","friday"], local_time:"09:00", timezone:"America/Los_Angeles", start_at:"2026-09-16T00:00:00Z", end_at:"2026-10-01T00:00:00Z"}, next_run_at:"2026-09-18T16:00:00Z", run_count:2, done_count:2};
  const writes = [], errors = [];
  let liveRuns = [];
  const provider = theme === "light"
    ? {provider_key:"analytics.gsc", provider_name:"Search Console", status:"free", estimated_usd:"0.00"}
    : {provider_key:"custom.api.crm", provider_name:"CRM", status:"creator_estimate", estimated_usd:"0.03", basis:"Three requests at $0.01 each on the standard plan.", pricing_url:"https://provider.example/pricing"};
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = data => {response.setHeader("Content-Type","application/json"); response.end(JSON.stringify(data));};
    if (url.pathname === "/") {
      response.setHeader("Content-Type","text/html");
      return response.end((await fs.readFile(path.join(assets,"index.html"),"utf8")).replaceAll("{{ASSET_VERSION}}","test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}","").replaceAll("{{BILLING_ENABLED}}","false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {const data = await fs.readFile(file); response.setHeader("Content-Type",file.endsWith(".js")?"text/javascript":file.endsWith(".css")?"text/css":"application/octet-stream"); return response.end(data);} catch {response.writeHead(404).end(); return;}
    }
    if (["POST","PATCH","PUT"].includes(request.method)) {
      let raw=""; for await(const chunk of request) raw+=chunk;
      const body=JSON.parse(raw || "{}"); writes.push({path:url.pathname, body});
      if (url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:["on_demand","daily","weekly"], input_schema:schema, can_run:true, can_schedule:true, prerequisites:[{kind:"artifact", path:"reports/INPUT.md",resolved_path:"reports/INPUT.md",revision:"a".repeat(40),satisfied:true,level:"required"}], connections:[{provider_key:provider.provider_key,ready:true}], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.00",basis:"included_bounded_compute",external_provider_cost:theme === "light" ? "free" : "estimated", external_providers:[provider]}});
      if (url.pathname.endsWith("/workflows/saved")) {
        if (body.expected_settings_revision !== configured.settings_revision) {
          response.statusCode=409; return send({detail:"Settings changed elsewhere"});
        }
        configured={...configured,...body,settings_revision:configured.settings_revision+1}; return send(configured);
      }
      response.statusCode=400; return send({detail:"Unexpected write"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
    if (url.pathname === "/api/projects/project/runs") return send(liveRuns);
    if (url.pathname.endsWith("/system")) return send({workflow_count:1,running_count:0,waiting_count:0,runs_this_month:2});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route("**/*",route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await context.addInitScript(theme=>{
      window.Clerk={load:async()=>{},isSignedIn:true,user:{id:"member"},session:{getToken:async()=>"synthetic-only"}};
      localStorage.setItem("tin-lite:theme",theme);
    },theme);
    const page=await context.newPage(); page.setDefaultTimeout(10000);
    page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    await page.getByRole("button",{name:"Open Weekly orders settings",exact:true}).click();
    await page.getByText("Included compute · 0 Tin credits",{exact:true}).waitFor();
    assert.match(await page.locator(".code-workflow-setup").innerText(), /reports\/INPUT.md: found in project Files/);
    assert.match(await page.locator(".code-workflow-setup").getByRole("link", {name:"Open", exact:true}).getAttribute("href"), /path=reports%2FINPUT.md&revision=a{40}/);
    assert.equal(await page.getByLabel("Saved notes",{exact:true}).isVisible(),false);
    assert.equal(await page.getByLabel("Saved notes",{exact:true}).inputValue(),"Remembered notes");
    // A thin chevron, not the browser's solid triangle, and it turns down when open.
    const chevron=()=>page.locator(".x-workflow-details > summary").evaluate(node=>{const mark=getComputedStyle(node,"::before");return {list:getComputedStyle(node).listStyleType,size:mark.width,stroke:mark.borderRightWidth,turn:mark.transform};});
    const closed=await chevron();
    assert.equal(closed.list,"none");
    assert.equal(closed.size,"6px");
    assert.notEqual(closed.stroke,"0px");
    await page.getByText("More options",{exact:true}).click();
    await page.waitForFunction(turn=>getComputedStyle(document.querySelector(".x-workflow-details > summary"),"::before").transform!==turn,closed.turn);
    assert.equal(await page.getByLabel("Saved notes",{exact:true}).isVisible(),true);
    await page.getByText("More options",{exact:true}).click();
    assert.equal(await page.getByRole("button",{name:"Weekly",exact:true}).isVisible(),true);
    assert.equal(await page.getByLabel("Monday",{exact:true}).isChecked(),true);
    assert.equal(await page.getByLabel("Friday",{exact:true}).isChecked(),true);
    assert.ok((await page.locator(".code-workflow-setup").innerText()).includes(`${provider.provider_name}: connected`));
    if (theme === "light") await page.getByText("Search Console API · Free", {exact:true}).waitFor();
    else {
      await page.locator(".code-workflow-setup summary").click();
      assert.match(await page.locator(".code-workflow-setup").innerText(), /Creator estimate: Three requests/);
      assert.equal(await page.getByRole("link", {name:"Provider pricing ↗"}).getAttribute("href"), "https://provider.example/pricing");
    }
    assert.doesNotMatch(await page.locator(".code-workflow-setup").innerText(), /Tin cannot estimate/);
    assert.match(await page.locator(".code-workflow-setup").innerText(),/Ends/);
    // The setup readout starts on the same edge as the settings above it and the footer below.
    const edges=await page.locator(".system-config-form").evaluate(form=>[".system-config-body .system-setting",".code-workflow-setup p",".system-config-footer > *"].map(selector=>Math.round(form.querySelector(selector).getBoundingClientRect().left)));
    assert.equal(new Set(edges).size,1,JSON.stringify(edges));
    await page.evaluate(() => {window.testOpenForm = document.querySelector(".system-config-form");});
    const setupCount = () => writes.filter(item => item.path.endsWith("/workflow-setup")).length;
    const initialChecks = setupCount();
    await page.evaluate(() => pollRuns());
    assert.equal(setupCount(), initialChecks, "an unchanged poll must not check setup again");
    assert.equal(await page.evaluate(() => window.testOpenForm === document.querySelector(".system-config-form")), true);
    await page.getByLabel("Minimum cents", {exact:true}).fill("2000");
    await page.waitForResponse(response => response.url().endsWith("/workflow-setup"));
    const editedChecks = setupCount();
    liveRuns = [{id:"live-run", project_workflow_id:"saved", workflow_id:"code", workflow_name:"custom.report", status:"running", created_at:"2026-09-30T12:00:00Z", progress_percent:25}];
    configured = {...configured,last_run_id:"live-run",last_run_status:"running"};
    await page.evaluate(() => pollRuns());
    assert.equal(setupCount(), editedChecks, "a changed run must preserve the setup check");
    assert.equal(await page.evaluate(() => window.testOpenForm === document.querySelector(".system-config-form")), true);
    assert.equal(await page.getByLabel("Minimum cents", {exact:true}).inputValue(), "2000");
    assert.equal(await page.getByLabel("Minimum cents", {exact:true}).evaluate(input => input === document.activeElement), true);
    assert.equal(await page.locator(".system-config-form .system-progress").getAttribute("aria-label"),"25% complete");
    await page.locator(".system-config-form").getByLabel("Name", {exact:true}).fill("Renamed report");
    await page.getByRole("button", {name:"On demand", exact:true}).click();
    await page.getByText("Setup ready to run.", {exact:true}).waitFor();
    await page.getByRole("button", {name:"Weekly", exact:true}).click();
    assert.equal(setupCount(), editedChecks, "name and schedule changes need no API estimate");
    for (const width of [1440, 390]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.locator(".code-workflow-setup").evaluate(node=>getComputedStyle(node).borderTopWidth),"1px");
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if (process.env.TIN_CODE_SCREENSHOTS) await page.screenshot({path:`${process.env.TIN_CODE_SCREENSHOTS}/code-setup-${theme}-${width}.png`,fullPage:true});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByLabel("Wednesday",{exact:true}).check();
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    assert.deepEqual(configured.schedule.weekdays,["monday","wednesday","friday"]);
    assert.equal(configured.schedule.start_at,"2026-09-16T00:00:00Z");
    assert.equal(configured.schedule.end_at,"2026-10-01T00:00:00Z");
    assert.equal(configured.inputs.minimum_cents,2000);
    assert.equal(configured.inputs.notes,"Remembered notes");
    await page.getByRole("button",{name:"Open Renamed report settings",exact:true}).click();
    await page.getByText("Included compute · 0 Tin credits",{exact:true}).waitFor();
    await page.getByLabel("Minimum cents",{exact:true}).fill("3000");
    configured = {...configured, settings_revision:3, inputs:{minimum_cents:4000}};
    liveRuns = liveRuns.map(run=>({...run,status:"succeeded"}));
    await page.evaluate(() => pollRuns());
    assert.equal(await page.evaluate(() => state.projectWorkflows[0].settings_revision),3);
    assert.equal(await page.locator(".system-config-form .system-progress").count(),0);
    assert.equal(await page.getByLabel("Minimum cents",{exact:true}).inputValue(),"3000");
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.getByText("These settings changed elsewhere. The latest version is now shown.",{exact:true}).waitFor();
    assert.equal(writes.filter(item=>item.path.endsWith("/workflows/saved")).at(-1).body.expected_settings_revision,2);
    assert.equal(configured.inputs.minimum_cents,4000,"an open editor must not overwrite a newer revision");
    assert.equal(writes.some(x=>x.path.includes("estimate") || x.path.endsWith("/runs")),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
});

test("social code workflow uses ordinary project file or text inputs without source discovery", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id:"project", name:"Fixture project", workspace_id:"workspace", workspace_name:"Fixture", timezone:"UTC", member_count:1};
  const {definition} = JSON.parse(await fs.readFile("workflow_packages/social.post_batch/workflow.json", "utf8"));
  const schema = definition.input_schema;
  const workflow = {id:"social", key:definition.key, title:definition.title, description:definition.description, version_label:definition.version, status:"active", executor:"workflow.code", allowed_actions:["start","save"], definition};
  const reads = [], writes = [], errors = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = data => {response.setHeader("Content-Type","application/json"); response.end(JSON.stringify(data));};
    if (url.pathname === "/") {
      response.setHeader("Content-Type","text/html");
      return response.end((await fs.readFile(path.join(assets,"index.html"),"utf8")).replaceAll("{{ASSET_VERSION}}","test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}","").replaceAll("{{BILLING_ENABLED}}","false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      try {const file=path.join(assets,url.pathname.slice(8)); const data=await fs.readFile(file); response.setHeader("Content-Type",file.endsWith(".js")?"text/javascript":file.endsWith(".css")?"text/css":"application/octet-stream"); return response.end(data);} catch {response.writeHead(404).end(); return;}
    }
    if (request.method === "POST" && url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:["on_demand"], input_schema:schema, can_run:true, can_schedule:false, connections:[], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.01",basis:"conservative_configured_bound",external_provider_cost:"not_applicable"}});
    if (request.method === "POST" && url.pathname === "/api/projects/project/workflows") {
      let raw=""; for await(const chunk of request) raw += chunk;
      const body=JSON.parse(raw); writes.push({path:url.pathname,body});
      return send({id:"saved", project_id:"project", workflow_id:"social", workflow_key:"social.post_batch", workflow_title:workflow.title, name:body.name, definition_commit_sha:"a".repeat(40), inputs:body.inputs, input_schema:schema, status:"active", settings_revision:1, created_at:"2026-09-28T00:00:00Z", schedule:null, run_count:0, done_count:0});
    }
    if (request.method === "POST" && url.pathname === "/api/projects/project/workflows/saved/runs") {
      writes.push({path:url.pathname}); response.statusCode=202;
      return send({id:"run", project_id:"project", workflow_id:"social", workflow_name:"social.post_batch", status:"pending", created_at:"2026-09-28T00:00:00Z"});
    }
    if (url.pathname.startsWith("/api/")) reads.push(url.pathname);
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([]);
    if (url.pathname === "/api/projects/project/files") return send({revision:"a".repeat(40),files:[{path:"reports/PUBLIC_ARTICLE.md"}]});
    if (url.pathname.endsWith("/system")) return send({workflow_count:0,running_count:0,waiting_count:0,runs_this_month:0});
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    await context.route("**/*",route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await context.addInitScript(()=>{window.Clerk={load:async()=>{},isSignedIn:true,user:{id:"member"},session:{getToken:async()=>"synthetic-only"}};});
    const page=await context.newPage(); page.setDefaultTimeout(10000);
    page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    await page.getByRole("button",{name:"Workflows",exact:true}).click();
    await page.getByRole("button",{name:"Set up",exact:true}).click();
    assert.equal(await page.locator("[data-approved-source-picker]").count(),0);
    assert.equal(await page.locator("[name='input:article_path']").count(),1);
    assert.equal(await page.locator("textarea[name='input:article_text']").count(),1);
    const article = page.locator("[data-project-file-input]").filter({has: page.locator("[name='input:article_path']")});
    await article.getByRole("button", {name:"Change",exact:true}).click();
    await article.getByRole("combobox").selectOption("reports/PUBLIC_ARTICLE.md");
    await article.getByRole("link", {name:"Open",exact:true}).waitFor();
    await page.getByText("Setup ready to run.", {exact:true}).waitFor();
    if (process.env.TIN_CODE_SCREENSHOTS) await page.screenshot({path:`${process.env.TIN_CODE_SCREENSHOTS}/project-files-inputs.png`,fullPage:true});
    await page.getByRole("button",{name:"Set up and run now"}).click();
    await page.getByText("Workflow added and started.").waitFor();
    assert.equal(reads.some(value=>value.includes("workflow-sources")),false);
    assert.equal(writes.length,2);
    assert.equal(writes[0].body.inputs.article_path,"reports/PUBLIC_ARTICLE.md");
    assert.equal(Object.hasOwn(writes[0].body.inputs,"source_run_id"),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
});

for (const theme of ["light", "dark"]) test(`project file controls preserve choices and show missing documents: ${theme}`, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 800}});
    await page.setContent(`<html data-theme="${theme}"><body><main class="workspace"><form class="system-template-card is-open"><div class="system-template-setup-body"><section><div class="x-workflow-fields" id="fields"></div></section></div></form></main></body></html>`);
    await page.addStyleTag({path: "src/tin_lite/static/app.css"});
    await page.addStyleTag({path: "src/tin_lite/static/x-posts.css"});
    await page.addScriptTag({path: "src/tin_lite/static/project-file-input.js"});
    await page.evaluate(() => {
      fields.innerHTML = `<div class="system-setting"><strong>Audience</strong>${TinProjectFileInput.field("input:audience", "context/AUDIENCE.md", "Audience", "audience", true)}</div>
        <details class="x-workflow-details"><summary>More options</summary><div class="system-setting"><strong>Source material</strong>${TinProjectFileInput.field("input:source", "context/missing.md", "Source material", "source", true)}</div></details>`;
      window.calls = 0; window.currentProject = true;
      TinProjectFileInput.bind(document.querySelector("form"), {
        projectId: "project", isCurrent: () => currentProject,
        api: async () => {calls++; return {revision: "a".repeat(40), files: [{path: "context/AUDIENCE.md"}, {path: "notes/release.md"}]};},
      });
    });
    await page.getByText("Found in project Files.", {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => calls), 1, "one listing shared by all controls");
    assert.equal(await page.locator("details").getAttribute("open"), "");
    assert.equal(await page.getByLabel("Source material", {exact: true}).inputValue(), "context/missing.md", "missing saved reference is never silently replaced");
    assert.equal(await page.getByRole("link", {name: "Open", exact: true}).getAttribute("target"), "_blank", "opening a document keeps unsaved setup intact");
    assert.match(await page.getByRole("link", {name: "Open", exact: true}).getAttribute("href"), /project=project&path=context%2FAUDIENCE.md&revision=a{40}/);
    // A native select draws our chevron, 9px in, not the platform's arrow.
    const select = await page.getByLabel("Source material", {exact: true}).evaluate(node => {
      const style = getComputedStyle(node);
      return {appearance: style.appearance, image: style.backgroundImage, position: style.backgroundPosition, padding: style.paddingRight};
    });
    assert.equal(select.appearance, "none");
    assert.match(select.image, /M1 1l4 4 4-4/);
    assert.equal(select.position, "calc(100% - 9px) 50%");
    assert.equal(select.padding, "28px");
    await page.getByLabel("Source material", {exact: true}).selectOption("notes/release.md");
    assert.equal(await page.getByRole("link", {name: "Open", exact: true}).count(), 2);
    await page.locator("[data-project-file-input]").first().getByRole("button", {name:"Change",exact:true}).click();
    assert.equal(await page.evaluate(() => calls), 2, "Change refreshes Files after upstream work");
    assert.equal(await page.locator('[name="input:source"]').inputValue(), "notes/release.md");
    for (const width of [1100, 390]) {
      await page.setViewportSize({width, height: 800});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.TIN_CODE_SCREENSHOTS) await page.screenshot({path: `${process.env.TIN_CODE_SCREENSHOTS}/file-setup-${theme}-${width}.png`, fullPage: true});
    }
    await page.evaluate(() => {
      fields.innerHTML += `<div id="stale">${TinProjectFileInput.field("input:late", "late.md", "Late file", "late")}</div>`;
      TinProjectFileInput.bind(document.querySelector("#stale"), {projectId: "project", isCurrent: () => currentProject, api: () => new Promise(resolve => {window.finishListing = resolve;})});
      currentProject = false;
      finishListing({revision: "b".repeat(40), files: [{path: "late.md"}]});
    });
    assert.equal(await page.locator("#stale [data-file-open]").isVisible(), false, "a stale response cannot open another project's file");
  } finally {await browser.close();}
});
