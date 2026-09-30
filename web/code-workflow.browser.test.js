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
  const schema = {type:"object", properties:{minimum_cents:{type:"integer", minimum:0, title:"Minimum cents"}}, required:["minimum_cents"]};
  const workflow = {id:"code", key:"custom.report", title:"Order report", description:"Fixture report", version_label:"1.0", status:"active", executor:"workflow.code", allowed_actions:["start","save"], definition:{executor:"workflow.code", input_schema:schema, schedule_modes:["on_demand"]}};
  let configured = {id:"saved", project_id:"project", workflow_id:"code", workflow_key:workflow.key, workflow_title:workflow.title, name:"Weekly orders", definition_commit_sha:"a".repeat(40), inputs:{minimum_cents:1000}, input_schema:schema, status:"active", settings_revision:1, created_at:"2026-09-16T00:00:00Z", schedule:{cadence:"weekly", weekdays:["monday","friday"], local_time:"09:00", timezone:"America/Los_Angeles", start_at:"2026-09-16T00:00:00Z", end_at:"2026-10-01T00:00:00Z"}, next_run_at:"2026-09-18T16:00:00Z", run_count:2, done_count:2};
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
      if (url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:["on_demand","daily","weekly"], input_schema:schema, can_run:true, can_schedule:true, connections:[{provider_key:provider.provider_key,ready:true}], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.00",basis:"included_bounded_compute",external_provider_cost:theme === "light" ? "free" : "estimated", external_providers:[provider]}});
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
    await page.evaluate(() => {window.testOpenForm = document.querySelector(".system-config-form");});
    const setupCount = () => writes.filter(item => item.path.endsWith("/workflow-setup")).length;
    const initialChecks = setupCount();
    await page.evaluate(() => pollRuns());
    assert.equal(setupCount(), initialChecks, "an unchanged poll must not check setup again");
    assert.equal(await page.evaluate(() => window.testOpenForm === document.querySelector(".system-config-form")), true);
    await page.getByLabel("Minimum cents", {exact:true}).fill("2000");
    await page.waitForResponse(response => response.url().endsWith("/workflow-setup"));
    const editedChecks = setupCount();
    liveRuns = [{id:"live-run", workflow_id:"code", workflow_name:"custom.report", status:"running", created_at:"2026-09-30T12:00:00Z"}];
    await page.evaluate(() => pollRuns());
    assert.equal(setupCount(), editedChecks, "a changed run must preserve the setup check");
    assert.equal(await page.evaluate(() => window.testOpenForm === document.querySelector(".system-config-form")), true);
    assert.equal(await page.getByLabel("Minimum cents", {exact:true}).inputValue(), "2000");
    assert.equal(await page.getByLabel("Minimum cents", {exact:true}).evaluate(input => input === document.activeElement), true);
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
    await page.getByRole("button",{name:"Open Renamed report settings",exact:true}).click();
    await page.getByText("Included compute · 0 Tin credits",{exact:true}).waitFor();
    await page.getByLabel("Minimum cents",{exact:true}).fill("3000");
    configured = {...configured, settings_revision:3, inputs:{minimum_cents:4000}};
    liveRuns = liveRuns.map(run=>({...run,status:"succeeded"}));
    await page.evaluate(() => pollRuns());
    assert.equal(await page.evaluate(() => state.projectWorkflows[0].settings_revision),3);
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
    await page.getByRole("button",{name:"Add workflows"}).click();
    await page.getByRole("button",{name:"Set up",exact:true}).click();
    assert.equal(await page.locator("[data-approved-source-picker]").count(),0);
    assert.equal(await page.locator("[name='input:article_path']").count(),1);
    assert.equal(await page.locator("textarea[name='input:article_text']").count(),1);
    await page.locator("[name='input:article_path']").fill("reports/PUBLIC_ARTICLE.md");
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
