// Packaged dashboard with synthetic HTTP responses; no LinkedIn requests.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

test("collection setup keeps cloud choices and recovers a saved workflow after start fails", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id:"project",name:"Example project",workspace_id:"workspace",timezone:"UTC",member_count:1};
  const schema = {type:"object",properties:{
    friends:{type:"array",title:"Friends' LinkedIn profile URLs",description:"Paste LinkedIn profile links, separated by commas.",items:{type:"string"}},
    keywords:{type:"string",title:"Keywords (optional)",description:"Narrow LinkedIn results with a term such as founder. Leave blank for no keyword filter.",default:""},
    execution:{type:"string",title:"Collection mode",enum:["local_only","cloud_only","cloud_preferred"],default:"local_only",description:"Choose where collection runs."},
  },required:["friends"]};
  const workflow = {id:"collection",key:"connections.collect",title:"Collect connections",description:"Collect visible connections.",version_label:"1",status:"active",executor:"connections.collect",allowed_actions:["start","save"],collection_availability:{cloud_ready:false},definition:{input_schema:schema,schedule_modes:["on_demand"]}};
  let saved = null, creates = 0;
  const errors = [];
  const server = http.createServer(async (request,response) => {
    const url = new URL(request.url,"http://localhost");
    const send = data => {response.setHeader("Content-Type","application/json");response.end(JSON.stringify(data));};
    if (url.pathname === "/") {
      response.setHeader("Content-Type","text/html");
      return response.end((await fs.readFile(path.join(assets,"index.html"),"utf8")).replaceAll("{{ASSET_VERSION}}","test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}","").replaceAll("{{BILLING_ENABLED}}","false"));
    }
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets,url.pathname.slice(8));
      try {const data = await fs.readFile(file);response.setHeader("Content-Type",file.endsWith(".js")?"text/javascript":file.endsWith(".css")?"text/css":"application/octet-stream");return response.end(data);} catch {response.writeHead(404).end();return;}
    }
    if (request.method === "POST" && url.pathname === "/api/projects/project/workflows") {
      let raw="";for await (const chunk of request) raw+=chunk;
      const body=JSON.parse(raw);creates++;
      const oldSchema=structuredClone(schema);
      for(const field of Object.values(oldSchema.properties)) {delete field.title;delete field.description;}
      saved={...body,id:"saved",project_id:"project",workflow_key:workflow.key,workflow_title:workflow.title,version_label:"1",definition_commit_sha:"a".repeat(40),input_schema:oldSchema,status:"active",settings_revision:1,created_at:"2026-10-01T00:00:00Z",run_count:0,done_count:0};
      return send(saved);
    }
    if (request.method === "POST" && url.pathname.endsWith("/runs")) {
      response.statusCode=409;return send({detail:"Cloud collection is not available on this deployment yet. Choose Local only."});
    }
    if (request.method === "PUT" && url.pathname.endsWith("/workflows/saved")) {
      let raw="";for await (const chunk of request) raw+=chunk;
      saved={...saved,...JSON.parse(raw),settings_revision:2};return send(saved);
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname.endsWith("/integrations")) return send([{key:"network.linkedin",name:"LinkedIn",status:"connected",configured:true,configuration:{collection_permission:{version:1,mode:"cloud_preferred"}}}]);
    if (url.pathname === "/api/projects/project/workflows") return send(saved?[saved]:[]);
    if (url.pathname.endsWith("/system")) return send({workflow_count:saved?1:0,running_count:0,waiting_count:0,runs_this_month:0});
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
    const page=await context.newPage();page.setDefaultTimeout(10000);
    page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    await page.getByRole("button",{name:"Workflows",exact:true}).click();
    await page.getByRole("button",{name:"Set up",exact:true}).click();
    const setup=page.locator(".system-template-card.is-open");
    await setup.getByLabel("Friends' LinkedIn profile URLs",{exact:true}).fill("https://www.linkedin.com/in/example-person");
    await setup.getByLabel("Keywords (optional)",{exact:true}).fill("founder");
    assert.match(await setup.locator("#template-input-keywords-help").innerText(),/Leave blank/);
    assert.match(await setup.locator("#template-input-execution-help").innerText(),/Browser collection needs Chrome open and awake/);
    assert.equal(await setup.locator('[name="input:execution"]').inputValue(),"cloud_preferred");
    await setup.getByRole("button",{name:"Collection mode",exact:true}).click();
    assert.equal(await setup.getByRole("option",{name:"Cloud only",exact:true}).isEnabled(),true);
    await setup.getByRole("option",{name:"Cloud preferred",exact:true}).click();
    if (process.env.TIN_COLLECTION_SCREENSHOT) await setup.screenshot({path:process.env.TIN_COLLECTION_SCREENSHOT});
    await setup.getByRole("button",{name:"Collection mode",exact:true}).click();
    await setup.getByRole("option",{name:"Cloud only",exact:true}).click();
    await setup.getByRole("button",{name:"Set up and run now",exact:true}).click();
    await page.getByText("Workflow saved, but it could not start:",{exact:false}).waitFor();
    const editor=page.locator(".system-config-form");
    await editor.waitFor();
    assert.equal(creates,1);
    assert.equal(await editor.locator('[name="input:execution"]').inputValue(),"cloud_only");
    assert.equal(await editor.getByLabel("Keywords (optional)",{exact:true}).inputValue(),"founder");
    assert.match(await editor.locator("#system-input-keywords-help").innerText(),/Leave blank/);
    assert.match(await editor.locator("#system-input-friends-help").innerText(),/profile links/);
    assert.equal(await editor.getByRole("button",{name:"Set up and run now",exact:true}).count(),0);
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    }
    await editor.getByRole("button",{name:"Collection mode",exact:true}).click();
    await editor.getByRole("option",{name:"Local only",exact:true}).click();
    await editor.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    assert.equal(creates,1);
    assert.equal(saved.inputs.execution,"local_only");
    assert.deepEqual(errors,[]);
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
});
