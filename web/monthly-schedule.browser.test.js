// Real packaged dashboard in Chromium; identity and HTTP responses are fixtures.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

test("a saved schedule switches to quarterly and back to weekly", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const project = {id:"project", name:"Fixture project", workspace_id:"workspace", workspace_name:"Fixture", timezone:"UTC", member_count:1};
  const schema = {type:"object", properties:{minimum_cents:{type:"integer", minimum:0, title:"Minimum cents"}}, required:["minimum_cents"]};
  const modes = ["on_demand","daily","weekly","monthly"];
  const workflow = {id:"code", key:"custom.report", title:"Order report", description:"Fixture report", version_label:"1.0", status:"active", executor:"workflow.code", allowed_actions:["start","save"], definition:{executor:"workflow.code", input_schema:schema, schedule_modes:modes}};
  let configured = {id:"saved", project_id:"project", workflow_id:"code", workflow_key:workflow.key, workflow_title:workflow.title, name:"Order report", definition_commit_sha:"a".repeat(40), inputs:{minimum_cents:1000}, input_schema:schema, status:"active", settings_revision:1, created_at:"2026-09-16T00:00:00Z", schedule:{cadence:"weekly", weekdays:["monday"], local_time:"09:00", timezone:"UTC", start_at:"2026-09-16T00:00:00Z", end_at:null}, next_run_at:"2026-10-05T09:00:00Z", run_count:0, done_count:0};
  const saves = [], errors = [];
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
      const body=JSON.parse(raw || "{}");
      if (url.pathname.endsWith("/workflow-setup")) return send({schedule_modes:modes, input_schema:schema, can_run:true, can_schedule:true, connections:[], issues:[], schedule_issues:[], estimate:{estimated_usd:"0.00",basis:"included_bounded_compute",external_provider_cost:"not_applicable"}});
      if (url.pathname.endsWith("/workflows/saved")) {
        saves.push(body);
        configured={...configured,...body,settings_revision:configured.settings_revision+1}; return send(configured);
      }
      response.statusCode=400; return send({detail:"Unexpected write"});
    }
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname === "/api/workflows") return send([workflow]);
    if (url.pathname === "/api/projects/project/workflows") return send([configured]);
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
    await context.addInitScript(()=>{
      window.Clerk={load:async()=>{},isSignedIn:true,user:{id:"member"},session:{getToken:async()=>"synthetic-only"}};
    });
    const page=await context.newPage(); page.setDefaultTimeout(10000);
    page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/?project=project#workflows`);
    const open = async () => {
      await page.getByRole("button",{name:"Open Order report settings",exact:true}).click();
      await page.getByText("Included compute · 0 Tin credits",{exact:true}).waitFor();
    };
    const form = () => page.locator(".system-config-form");
    await open();
    assert.equal(await form().getByRole("button",{name:"Day of the month",exact:true}).isVisible(),false);

    await page.getByRole("button",{name:"Monthly",exact:true}).click();
    assert.equal(await form().getByRole("button",{name:"Day of the month",exact:true}).isVisible(),true);
    assert.equal(await form().locator(".schedule-weekday").first().isVisible(),false);
    await form().getByRole("button",{name:"Day of the month",exact:true}).click();
    await form().getByRole("option",{name:"15th",exact:true}).click();
    await form().getByRole("button",{name:"Months",exact:true}).click();
    await form().getByRole("option",{name:"Jan, Apr, Jul, Oct",exact:true}).click();
    for (const width of [1440, 390]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      if (process.env.TIN_SCHEDULE_SCREENSHOTS) await form().screenshot({path:`${process.env.TIN_SCHEDULE_SCREENSHOTS}/monthly-${width}.png`});
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    assert.deepEqual(saves.at(-1).schedule, {cadence:"monthly", weekdays:[], day_of_month:15, months:[1,4,7,10], local_time:"09:00", timezone:"UTC", start_at:"2026-09-16T00:00:00Z", end_at:null});
    await page.getByText("the 15th of Jan, Apr, Jul, Oct · 09:00",{exact:false}).first().waitFor();

    // Reopening shows the saved months; switching back to weekly drops the monthly fields.
    await open();
    assert.equal(await form().getByRole("button",{name:"Months",exact:true}).innerText(),"Jan, Apr, Jul, Oct");
    await page.getByRole("button",{name:"Weekly",exact:true}).click();
    await page.getByLabel("Thursday",{exact:true}).check();
    await page.getByRole("button",{name:"Save changes",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector(".system-config-form"));
    const weekly = saves.at(-1).schedule;
    assert.equal(weekly.cadence,"weekly");
    assert.equal("day_of_month" in weekly || "months" in weekly,false);
    assert.deepEqual(errors,[]);
  } finally {await browser.close(); await new Promise(resolve=>server.close(resolve));}
});
