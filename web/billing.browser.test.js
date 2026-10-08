// Packaged UI and synthetic transport only. No provider credentials or real payments.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

const assets = path.resolve("src/tin_lite/static");
async function harness({theme="light", admin=true, multiple=false, returning=admin, returnStatus=200, accessible=true, paymentStatus="pending", billingEnabled=true, welcome=false, entry="/billing"}={}) {
  const writes=[], errors=[], reads=[];
  const project={id:"project",name:"ClawMessenger",workspace_id:"workspace",workspace_name:"ClawMessenger",timezone:"UTC",member_count:2};
  const other={...project,id:"other",name:"Other project",workspace_id:"other-workspace",workspace_name:"Other workspace"};
  const data={enabled:true,mode:"test",currency:"USD",is_admin:admin,workspace_id:"workspace",project_id:"project",status:"active",
    available_usd:"25.00",reserved_usd:"2.00",spent_this_month_usd:"1.50",topup_min_cents:1000,topup_max_cents:100000,
    policies:[{id:"project",name:"ClawMessenger",revision:1,per_run_nanos:10e9,monthly_nanos:100e9,schedule_max_nanos:null}],transactions:[]};
  if(!admin) {delete data.available_usd; delete data.reserved_usd;}
  if(welcome) {
    data.available_usd="10.00"; data.reserved_usd="0.00"; data.run_billing_enabled=false;
    data.transactions=[{id:1,kind:"welcome_credit",amount_usd:"10.00",created_at:"2026-09-14T12:00:00Z"}];
  }
  const server=http.createServer(async(request,response)=>{
    const url=new URL(request.url,"http://localhost");
    if(request.method==="GET" && url.pathname.startsWith("/api/")) reads.push(url.pathname);
    const send=(value,type="application/json")=>{response.setHeader("Content-Type",type);response.end(type.includes("json")?JSON.stringify(value):value);};
    if(url.pathname.startsWith("/assets/")) {
      const name=url.pathname.slice(8);
      if(name.includes("..")) {response.writeHead(400).end();return;}
      return send(await fs.readFile(path.join(assets,name)),name.endsWith(".js")?"text/javascript":name.endsWith(".css")?"text/css":"application/octet-stream");
    }
    if(["/", "/system", "/billing", "/activity"].includes(url.pathname)) return send((await fs.readFile(path.join(assets,"index.html"),"utf8"))
      .replace(/<script\b[^>]*src="\{\{CLERK[^>]+>[\s\S]*?<\/script>/g,"").replaceAll("{{ASSET_VERSION}}","test").replaceAll("{{BILLING_ENABLED}}",String(billingEnabled)),"text/html");
    if(request.method!=="GET") {
      let body="";for await(const chunk of request)body+=chunk;
      writes.push({path:url.pathname,body:JSON.parse(body||"{}")});
      if(url.pathname.endsWith("/limits")) return send({revision:2});
      if(url.pathname.endsWith("/checkout")) return send({id:"payment",status:"pending",checkout_url:null});
      response.statusCode=400; return send({detail:"Unexpected write"});
    }
    if(url.pathname==="/api/projects")return send(multiple?[other,...(accessible?[project]:[])]:[project]);
    if(url.pathname==="/api/billing/payments/payment/return") {
      response.statusCode=returnStatus;
      return send(returnStatus===200?{workspace_id:"workspace"}:{detail:"billing resource not found"});
    }
    if(url.pathname==="/api/projects/other/billing")return send({enabled:false,mode:"disabled"});
    if(url.pathname.endsWith("/billing/payments"))return send([{id:"payment",status:paymentStatus,amount_usd:"10.00",checkout_url:null,
      invoice_url:paymentStatus==="paid"?"https://invoice.stripe.com/i/test-fixture":null,refundable_usd:paymentStatus==="paid"?"10.00":"0.00"}]);
    if(url.pathname.endsWith("/billing"))return send(data);
    if(url.pathname.endsWith("/system"))return send({workflow_count:0,running_count:0,runs_this_month:0,waiting_count:0});
    if(url.pathname.startsWith("/api/"))return send([]);
    response.writeHead(404).end();
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.route("**/*",route=>route.request().url().startsWith(base)?route.continue():route.abort());
  await context.addInitScript(({theme,multiple})=>{
    window.Clerk={load:async()=>{},isSignedIn:true,user:{id:"member",firstName:"QA"},session:{getToken:async()=>"synthetic-only"}};
    localStorage.setItem("tin-lite:theme",theme);
    if(multiple) localStorage.setItem("tin-lite:project:member","other");
  },{theme,multiple});
  const page=await context.newPage();page.setDefaultTimeout(10000);
  page.on("pageerror",error=>errors.push(error.message));
  try {
    await page.goto(`${base}${entry}?${returning?"billing_payment=payment&":""}${multiple?"project=other":""}`);
    if (billingEnabled && entry === "/billing") {
      await page.locator(returning && (returnStatus!==200 || !accessible)?"[data-payment-retry]":".billing-amount").waitFor();
    } else {
      await page.waitForFunction(() => document.querySelector("#project-name")?.textContent === "ClawMessenger");
      await page.waitForFunction(() => !document.querySelector("#main .view-loading"));
    }
  } catch(error) {
    await browser.close();await new Promise(resolve=>server.close(resolve));
    throw new Error(`${error.message}; page errors: ${errors.join(", ")}`);
  }
  return {page,writes,errors,data,reads,base,async close(){await browser.close();await new Promise(resolve=>server.close(resolve));}};
}

test("Billing opens from System, refreshes directly, and supports back/forward without hashes", async () => {
  const f = await harness({entry: "/system", returning: false, welcome: true});
  try {
    await f.page.locator("#project-switcher").click();
    await f.page.getByRole("button", {name: "Billing →", exact: true}).click();
    await f.page.locator(".billing-amount").waitFor();
    assert.equal(new URL(f.page.url()).pathname, "/billing");
    assert.equal(new URL(f.page.url()).hash, "");
    assert.match(await f.page.locator(".billing-amount").innerText(), /10\.00/);
    await f.page.goBack();
    assert.equal(new URL(f.page.url()).pathname, "/system");
    await f.page.goForward();
    await f.page.locator(".billing-amount").waitFor();
    await f.page.reload();
    await f.page.locator(".billing-amount").waitFor();
    // Previously shared bookmarks still resolve to the same project and page.
    await f.page.goto(`${f.base}/?project=project#billing`);
    await f.page.locator(".billing-amount").waitFor();
    assert.equal(new URL(f.page.url()).pathname, "/billing");
    assert.equal(new URL(f.page.url()).searchParams.get("project"), "project");
    assert.equal(new URL(f.page.url()).hash, "");
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.errors, []);
  } finally { await f.close(); }
});

test("Scheduled spending can be enabled and disabled in the existing limit editor",async()=>{
  const f=await harness();
  try {
    await f.page.getByRole("button",{name:"Edit →",exact:true}).click();
    assert.equal(await f.page.getByLabel("Per scheduled run (USD)").inputValue(),"");
    await f.page.getByLabel("Per scheduled run (USD)").fill("2");
    await f.page.getByRole("button",{name:"Save limits",exact:true}).click();
    await f.page.waitForFunction(()=>!document.querySelector(".billing-editor"));
    assert.equal(f.writes.at(-1).body.schedule_max_nanos,2e9);
    f.data.policies[0].schedule_max_nanos=2e9;
    await f.page.getByRole("button",{name:"Edit →",exact:true}).click();
    await f.page.getByLabel("Per scheduled run (USD)").fill("");
    await f.page.getByRole("button",{name:"Save limits",exact:true}).click();
    await f.page.waitForFunction(()=>!document.querySelector(".billing-editor"));
    assert.equal(f.writes.at(-1).body.schedule_max_nanos,null);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("Self-hosted mode ignores stale billing links and never calls payment APIs",async()=>{
  const f=await harness({billingEnabled:false});
  try {
    assert.equal(f.reads.some(p=>p.includes("billing")),false);
    assert.equal(await f.page.locator(".billing-view").count(),0);
    await f.page.getByRole("button",{name:"Current project: ClawMessenger"}).click();
    assert.equal(await f.page.getByRole("button",{name:/^(Billing|Spending) →$/}).count(),0);
    assert.deepEqual(f.errors,[]);
    assert.equal(f.writes.length,0);
  } finally {await f.close();}
});

for(const paymentStatus of ["paid","pending","expired"])test(`Checkout ${paymentStatus} return overrides another selected workspace, only once`,async()=>{
  const f=await harness({multiple:true,paymentStatus});
  try {
    assert.equal(new URL(f.page.url()).searchParams.get("project"),"project");
    assert.equal(new URL(f.page.url()).searchParams.has("billing_payment"),false);
    assert.equal(new URL(f.page.url()).pathname,"/billing");
    assert.equal(new URL(f.page.url()).hash,"");
    assert.match(await f.page.locator(".billing-heading").innerText(),/ClawMessenger/);
    assert.equal(await f.page.evaluate(()=>localStorage.getItem("tin-lite:project:member")),"project");
    assert.equal(f.reads.includes("/api/projects/other/billing"),false);
    assert.equal(f.writes.length,0,"return cannot fund an account or start a workflow");
    await f.page.reload();
    await f.page.locator(".billing-amount").waitFor();
    assert.equal(f.reads.filter(p=>p.endsWith("/payment/return")).length,1);
    await f.page.getByRole("button",{name:"Current project: ClawMessenger"}).click();
    await f.page.locator('[data-project-id="other"]').click();
    await f.page.getByText("Billing is not enabled for this workspace.",{exact:true}).waitFor();
    assert.equal(new URL(f.page.url()).searchParams.get("project"),"other");
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

for(const options of [{returnStatus:404},{returnStatus:503},{accessible:false}])test(`Unavailable Checkout return does not open unrelated billing: ${JSON.stringify(options)}`,async()=>{
  const f=await harness({multiple:true,...options});
  try {
    assert.match(await f.page.locator(".view-error").innerText(),/does not mean your payment failed/);
    assert.equal(f.reads.includes("/api/projects/other/billing"),false);
    assert.equal(f.writes.length,0);
    assert.equal(new URL(f.page.url()).searchParams.get("billing_payment"),"payment");
    await f.page.getByRole("button",{name:"Open Tin",exact:true}).click();
    await f.page.getByText("Billing is not enabled for this workspace.",{exact:true}).waitFor();
    assert.equal(new URL(f.page.url()).searchParams.has("billing_payment"),false);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

for(const theme of ["light","dark"])test(`billing ${theme}: unfinished checkout stays out of Transactions`,async()=>{
  const f=await harness({theme});
  try {
    assert.equal(await f.page.locator(".billing-amount").innerText(),"$25.00");
    assert.equal(await f.page.locator(".billing-transaction").count(),0);
    assert.equal(await f.page.getByText("No transactions yet.",{exact:true}).count(),1);
    assert.equal(f.writes.length,0,"return from Stripe never starts a workflow or credits funds");
    await f.page.getByRole("button",{name:"Edit →",exact:true}).click();
    await f.page.getByLabel("Monthly limit (USD)").fill("75");
    assert.equal(await f.page.getByLabel("Monthly limit (USD)").inputValue(),"75");
    await f.page.getByRole("button",{name:"Save limits",exact:true}).click();
    await f.page.locator(".billing-editor").waitFor({state:"detached"});
    assert.equal(f.writes[0].body.monthly_nanos,75e9);
    assert.equal(f.writes[0].body.expected_revision,1);
    await f.page.evaluate(()=>document.fonts.ready);
    await f.page.screenshot({path:`/tmp/tin-billing-${theme}-1440.png`,fullPage:true});
    await f.page.setViewportSize({width:390,height:844});
    assert.equal(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(await f.page.locator(".billing-policy-head").isVisible(),false);
    await f.page.screenshot({path:`/tmp/tin-billing-${theme}-390.png`,fullPage:true});
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("unfinished checkout does not obscure real credits or charges",async()=>{
  const f=await harness({welcome:true});
  try {
    assert.equal(await f.page.locator(".billing-transaction").count(),1);
    assert.match(await f.page.locator(".billing-transactions").innerText(),/Welcome credits/);
    assert.doesNotMatch(await f.page.locator(".billing-transactions").innerText(),/Payment pending/);
    f.data.transactions.unshift(
      {id:3,kind:"charge",amount_usd:"-0.50",created_at:"2026-09-14T14:00:00Z"},
      {id:2,kind:"topup",amount_usd:"10.00",created_at:"2026-09-14T13:00:00Z"}
    );
    await f.page.getByRole("button",{name:"Refresh",exact:true}).click();
    await f.page.waitForFunction(()=>document.querySelectorAll(".billing-transaction").length===3);
    assert.match(await f.page.locator(".billing-transactions").innerText(),/Workflow run/);
    assert.match(await f.page.locator(".billing-transactions").innerText(),/Funds added/);
    assert.equal(f.writes.length,0);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("welcome credit is clear and self-service refunds are absent",async()=>{
  const f=await harness({welcome:true,paymentStatus:"paid"});
  try {
    assert.equal(await f.page.locator(".billing-amount").innerText(),"$10.00");
    assert.match(await f.page.locator(".billing-transactions").innerText(),/Welcome credits/);
    assert.match(await f.page.locator(".billing-balance").innerText(),/charging is not enabled/);
    assert.equal(await f.page.getByRole("button",{name:/refund/i}).count(),0);
    assert.equal(await f.page.locator("[data-refund-editor]").count(),0);
    assert.equal(f.writes.length,0);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("project member gets Spending without workspace payment controls",async()=>{
  const f=await harness({admin:false});
  try {
    assert.equal(await f.page.locator(".billing-heading h1").innerText(),"Spending");
    assert.equal(await f.page.locator(".billing-amount").innerText(),"$1.50");
    assert.equal(await f.page.locator(".billing-topup, #billing-invoices, [data-limits]").count(),0);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

for(const theme of ["light","dark"])test(`paid top-ups align with the section heading in ${theme} mode`,async()=>{
  const f=await harness({theme,paymentStatus:"paid"});
  try {
    for(const width of [1440,390]) {
      await f.page.setViewportSize({width,height:1000});
      const heading=await f.page.locator("#billing-invoices > h2").boundingBox();
      const amount=await f.page.locator("#billing-invoices > .billing-account-row > strong").boundingBox();
      assert.equal(amount.x,heading.x,"unboxed top-up amount aligns with its heading");
      const card=await f.page.locator(".billing-account").boundingBox();
      const cardLabel=await f.page.locator(".billing-account .billing-account-row > strong").first().boundingBox();
      assert.ok(cardLabel.x>card.x+19,"boxed account rows retain their inset");
      assert.equal(await f.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await f.page.screenshot({path:`/tmp/tin-billing-topup-${theme}-${width}.png`,fullPage:true});
    }
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("starts need no quote or hold dialog and recover a lost response",async()=>{
  const f=await harness();
  try {
    await f.page.evaluate(()=>{
      window.testCalls=[];window.testAttempts=0;
      window.tryPaidStart=()=>TinBilling.paidStart({path:"/api/workflows/workflow/runs",options:{method:"POST",body:JSON.stringify({project_id:"project",inputs:{brief:"private text"}})},
        projectId:"project",actor:"member",assertContext(){},async fetch(path,options){
          testCalls.push({path,options});
          testAttempts++;
          if(testAttempts===1)throw new TypeError("Connection lost after acceptance");
          return new Response(JSON.stringify({id:"same-run"}));
        }}).then(async response=>({result:await response.json()}),error=>({error:error.message}));
      window.testResult=tryPaidStart();
    });
    assert.match((await f.page.evaluate(()=>testResult)).error,/Connection lost/);
    assert.equal(await f.page.locator("dialog[open]").count(),0);
    assert.equal(await f.page.getByText(/reserved/).count(),0);
    assert.equal(await f.page.evaluate(()=>JSON.stringify(sessionStorage).includes("private text")),false);
    const retried=await f.page.evaluate(()=>tryPaidStart());
    assert.equal(retried.result.id,"same-run");
    const calls=await f.page.evaluate(()=>testCalls);
    assert.equal(calls.filter(c=>c.path.endsWith("/quotes")).length,0);
    assert.equal(calls[0].options.headers["Idempotency-Key"],calls[1].options.headers["Idempotency-Key"]);
    assert.equal(calls[0].options.body,calls[1].options.body);
    assert.equal(JSON.parse(calls[0].options.body).billing_quote_id,undefined);
    await f.page.evaluate(()=>tryPaidStart());
    assert.equal(await f.page.evaluate(()=>testAttempts),3);
    assert.notEqual((await f.page.evaluate(()=>testCalls))[2].options.headers["Idempotency-Key"],calls[0].options.headers["Idempotency-Key"]);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});

test("configuration cost preview reuses unchanged inputs and ignores stale replies",async()=>{
  const f=await harness();
  try {
    await f.page.evaluate(()=>{
      const form=document.createElement("form");
      form.innerHTML='<section class="system-config-when"><input name="scope" value="5"></section>';
      document.querySelector("#main").append(form);
      window.costForm=form;window.costCalls=[];window.costReplies=[];
      TinBilling.bindEstimate(form, {projectId:"project",isCurrent:()=>true,
        request:()=>({workflow_id:"workflow",inputs:{max_cost_usd:Number(form.elements.scope.value)}}),
        fetch:async(path,options)=>new Promise(resolve=>{costCalls.push({path,body:JSON.parse(options.body)});costReplies.push(resolve);}),
      });
    });
    await f.page.evaluate(()=>costForm.dispatchEvent(new Event("change")));
    await f.page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,30)));
    assert.equal(await f.page.evaluate(()=>costCalls.length),1);
    await f.page.evaluate(()=>costReplies[0](new Response(JSON.stringify({estimated_usd:"5.00",maximum_usd:"5.00",estimate:{basis:"conservative_configured_bound"}}))));
    await f.page.getByText("Maximum charge: $5.00 per run · actual usage is charged",{exact:true}).waitFor();
    await f.page.screenshot({path:"/tmp/tin-workflow-maximum-charge.png",fullPage:true});
    await f.page.locator('[name="scope"]').fill("6");
    await f.page.locator('[name="scope"]').dispatchEvent("change");
    await f.page.waitForFunction(()=>costCalls.length===2);
    await f.page.locator('[name="scope"]').fill("7");
    await f.page.locator('[name="scope"]').dispatchEvent("change");
    await f.page.waitForFunction(()=>costCalls.length===3);
    await f.page.evaluate(()=>{
      costReplies[2](new Response(JSON.stringify({estimated_usd:"1.50",maximum_usd:"7.00"})));
      costReplies[1](new Response(JSON.stringify({estimated_usd:"1.25",maximum_usd:"6.00"})));
    });
    await f.page.getByText("Usually about $1.50 per run, never more than $7.00 · actual usage is charged",{exact:true}).waitFor();
    assert.equal(await f.page.evaluate(()=>costCalls.every(c=>c.path.endsWith("/billing/estimate"))),true);
    assert.equal(await f.page.getByText(/\$6.00/).count(),0);
    assert.deepEqual(f.errors,[]);
  } finally {await f.close();}
});
