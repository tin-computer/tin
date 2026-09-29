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
      if (url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:["on_demand","daily","weekly"], input_schema:schema, can_run:true, can_schedule:true, connections:[{provider_key:"custom.api.crm",ready:true}], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.00",basis:"included_bounded_compute",external_provider_cost:"unknown"}});
      if (url.pathname.endsWith("/workflows/saved")) {configured={...configured,...body,settings_revision:2}; return send(configured);}
      response.statusCode=400; return send({detail:"Unexpected write"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
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
    assert.match(await page.locator(".code-workflow-setup").innerText(),/custom.api.crm: connected/);
    assert.match(await page.locator(".code-workflow-setup").innerText(),/Ends/);
    for (const width of [1440, 390]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.locator(".code-workflow-setup").evaluate(node=>getComputedStyle(node).borderTopWidth),"1px");
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if (process.env.TIN_CODE_SCREENSHOTS) await page.screenshot({path:`${process.env.TIN_CODE_SCREENSHOTS}/code-setup-${theme}-${width}.png`,fullPage:true});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByLabel("Minimum cents",{exact:true}).fill("2000");
    await page.getByLabel("Wednesday",{exact:true}).check();
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    assert.deepEqual(configured.schedule.weekdays,["monday","wednesday","friday"]);
    assert.equal(configured.schedule.start_at,"2026-09-16T00:00:00Z");
    assert.equal(configured.schedule.end_at,"2026-10-01T00:00:00Z");
    assert.equal(configured.inputs.minimum_cents,2000);
    assert.equal(writes.some(x=>x.path.includes("estimate") || x.path.endsWith("/runs")),false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
});

test("saved code workflow chooses verified sources from its pinned definition", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id:"project", name:"Fixture project", workspace_id:"workspace", workspace_name:"Fixture", timezone:"UTC", member_count:1};
  const schema = {type:"object", properties:{posts_run_id:{type:"string",format:"uuid",title:"Reviewed posts"},optional_run_id:{type:"string",format:"uuid",title:"Optional notes"},minimum_cents:{type:"integer",title:"Minimum cents"}}, required:["posts_run_id","minimum_cents"]};
  const workflow = {id:"code", key:"custom.launch_copy", title:"Launch copy", description:"Fixture source consumer", version_label:"2.0", status:"active", executor:"workflow.code", allowed_actions:["start","save"], definition:{executor:"workflow.code", input_schema:schema, schedule_modes:["on_demand"], code:{evidence:{posts:{kind:"approved_output",input:"posts_run_id",workflow_key:"social.post_batch",max_bytes:64000},notes:{kind:"approved_output",input:"optional_run_id",workflow_key:"research.deep_dive",max_bytes:64000}}}}};
  let configured = {id:"saved", project_id:"project", workflow_id:"code", workflow_key:workflow.key, workflow_title:workflow.title, name:"Launch copy", version_label:"1.0", definition_commit_sha:"a".repeat(40), inputs:{posts_run_id:"stale-source",minimum_cents:10}, input_schema:schema, status:"active", settings_revision:1, created_at:"2026-09-16T00:00:00Z", schedule:null, run_count:0, done_count:0};
  const approved = {run_id:"11111111-1111-4111-8111-111111111111", title:"Four reviewed posts", workflow_key:"social.post_batch", artifact_path:"reports/SOCIAL_POST_BATCH.md", revision:"b".repeat(40), created_at:"2026-09-25T00:00:00Z", read_url:"https://app.tin.computer/document/11111111-1111-4111-8111-111111111111?project=project"};
  let candidateEnabled = false;
  const sourceReads = [], writes = [], errors = [];
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
    if (request.method === "POST" && url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:["on_demand"], input_schema:schema, can_run:true, can_schedule:false, connections:[], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.00",basis:"included_bounded_compute",external_provider_cost:"not_applicable"}});
    if (request.method === "PUT" && url.pathname.endsWith("/workflows/saved")) {
      let raw=""; for await (const chunk of request) raw += chunk;
      const body=JSON.parse(raw); writes.push(body); configured={...configured,...body,settings_revision:2}; return send(configured);
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
    if (url.pathname === "/api/projects/project/workflow-sources/code") {
      sourceReads.push(url.searchParams.get("project_workflow_id"));
      return send({definition_revision:configured.definition_commit_sha, slots:[{slot:"posts",input:"posts_run_id",required:true,workflow_key:"social.post_batch",candidates:candidateEnabled?[approved]:[],missing_reason:candidateEnabled?null:"Review an existing draft in Decisions first."},{slot:"notes",input:"optional_run_id",required:false,workflow_key:"research.deep_dive",candidates:[],missing_reason:"No optional notes available."}]});
    }
    if (url.pathname.endsWith("/system")) return send({workflow_count:1,running_count:0,waiting_count:0,runs_this_month:0});
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
    await page.getByRole("button",{name:"Open Launch copy settings",exact:true}).click();
    await page.getByText("Previously selected source is no longer eligible. Choose another reviewed result.").waitFor();
    assert.equal(await page.locator("[data-approved-source-picker=posts]").inputValue(),"");
    assert.match(await page.locator(".code-workflow-setup").innerText(),/Choose an approved project result/);
    await page.getByRole("button",{name:"Close Launch copy settings",exact:true}).click();
    candidateEnabled=true;
    await page.getByRole("button",{name:"Open Launch copy settings",exact:true}).click();
    const picker=page.locator("[data-approved-source-picker=posts]");
    await picker.waitFor();
    assert.equal(await picker.locator("option").count(),2);
    await picker.selectOption(approved.run_id);
    assert.equal(await page.locator("[data-approved-source-picker=notes]").inputValue(),"");
    assert.equal(await page.getByLabel("Minimum cents",{exact:true}).getAttribute("type"),"number");
    assert.equal(await page.getByRole("link",{name:"Read reviewed copy"}).getAttribute("href"),approved.read_url);
    await page.getByText("Setup ready to run.").waitFor();
    if (process.env.TIN_APPROVED_SOURCE_SCREENSHOT) await page.screenshot({path:process.env.TIN_APPROVED_SOURCE_SCREENSHOT,fullPage:true});
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    assert.equal(configured.inputs.posts_run_id,approved.run_id);
    assert.equal(configured.inputs.minimum_cents,10);
    assert.equal(Object.hasOwn(configured.inputs,"optional_run_id"),false);
    assert.ok(sourceReads.length >= 2 && sourceReads.every(value=>value === "saved"));
    const edgeCases=await page.evaluate(async (source) => {
      const slots=[{slot:"posts",input:"posts_run_id",required:true,workflow_key:"social.post_batch",candidates:[source]}];
      const services={workflow:{id:"code"},configured:{id:"saved"},projectId:"project",isCurrent:()=>true,fetch:async()=>({ok:true,json:async()=>({slots})})};
      const ledger=document.createElement("form");
      ledger.dataset.workflowField="input:minimum_cents";
      ledger.innerHTML='<input name="value" value="10"><button type="submit">Save</button>';
      document.body.append(ledger);
      await window.TinCodeSetup.bindSources(ledger,services);
      const unrelated=ledger.elements.value.tagName;
      ledger.remove();
      const retry=document.createElement("form");
      retry.innerHTML='<input name="input:posts_run_id"><button type="submit">Save</button>';
      document.body.append(retry);
      let attempts=0;
      await window.TinCodeSetup.bindSources(retry,{...services,fetch:async()=>{
        if (++attempts===1) throw new Error("offline");
        return {ok:true,json:async()=>({slots})};
      }});
      const disabledAfterFailure=retry.querySelector("[type=submit]").disabled;
      retry.querySelector("[data-source-error] button").click();
      await new Promise(resolve=>setTimeout(resolve,30));
      const recovered=retry.querySelector("[data-approved-source-picker=posts]")?.tagName;
      retry.remove();
      const bounded=document.createElement("form");
      bounded.innerHTML='<input name="input:posts_run_id"><button type="submit">Save</button>';
      document.body.append(bounded);
      await window.TinCodeSetup.bindSources(bounded,{...services,fetch:async()=>({ok:false,status:409,json:async()=>({detail:"Recent approved-source search reached its 200-run limit"})})});
      const scanLimitMessage=bounded.querySelector("[data-source-error]").textContent;
      bounded.remove();
      const stale=document.createElement("form");
      stale.innerHTML='<input name="input:posts_run_id"><button type="submit">Save</button>';
      document.body.append(stale);
      let finish; let current=true;
      const pending=window.TinCodeSetup.bindSources(stale,{...services,isCurrent:()=>current,fetch:()=>new Promise(resolve=>{finish=resolve;})});
      current=false;
      finish({ok:true,json:async()=>({slots})});
      await pending;
      const staleControl=stale.elements.namedItem("input:posts_run_id").tagName;
      stale.remove();
      return {unrelated,disabledAfterFailure,recovered,staleControl,scanLimitMessage};
    },approved);
    assert.deepEqual(edgeCases,{unrelated:"INPUT",disabledAfterFailure:true,recovered:"SELECT",staleControl:"INPUT",scanLimitMessage:"Recent approved-source search reached its 200-run limit Retry source choices"});
    assert.deepEqual(errors,[]);
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
});
