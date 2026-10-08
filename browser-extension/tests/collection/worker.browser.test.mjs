import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, cp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { chromium } from "playwright";
import { selection, profileHTML, resultsHTML, person } from "./fixtures.mjs";

const extensionPath = new URL("../..",import.meta.url).pathname;
for (const mode of ["legacy", "handoff", "automatic", "missing_account"]) test(mode === "missing_account" ? "a missing account on the prepared page pauses without claiming the saved login expired" : mode === "automatic" ? "paired MV3 worker collects a queued run without opening the popup" : mode === "handoff" ? "cloud cleanup handoff resumes automatically in Chrome without another transfer" : "real MV3 pairing, popup closure, worker restart and lost page acknowledgement", {timeout:90000}, async t => {
  const handoff = mode === "handoff", missingAccount = mode === "missing_account", automatic = mode === "automatic" || missingAccount;
  const dir = await mkdtemp(join(tmpdir(),"tin-collection-mv3-"));
  t.after(() => rm(dir,{recursive:true,force:true}));
  let pages=[], accepted=handoff?1:0, failOnce=!handoff, paired=false;
  const job = {run_id:"00000000-0000-4000-8000-000000000001", inputs:{friends:[selection.friend.profile_url],keywords:"founder",execution:"local_only"}, actor:selection.actor, state:"waiting_browser", generation:1, friend_index:0, next_page:1, cloud_transport:null,execution_mode:"local"};
  if(handoff) Object.assign(job,{state:"handoff_pending",execution_mode:"cloud",cloud_transport:"http_v1",next_page:2,inputs:{...job.inputs,execution:"cloud_preferred"}});
  if(missingAccount) Object.assign(job,{cloud_transport:"http_v2",reason:"browser_preparation_required",inputs:{...job.inputs,execution:"cloud_preferred"}});
  const server=createServer(async(req,res)=>{
    let raw="";for await(const chunk of req) raw+=chunk;
    const body=raw?JSON.parse(raw):null;
    res.setHeader("Content-Type","application/json");res.setHeader("Access-Control-Allow-Origin","*");
    if(req.url==="/"){res.setHeader("Content-Type","text/html");res.end("<!doctype html><title>Tin fixture</title><h1>Connect</h1>");return;}
    if(req.url==="/api/connection-extension/pair") {
      assert.equal(body.grant,"synthetic-pairing-grant-12345678901234567890");assert.match(body.token_hash,/^[a-f0-9]{64}$/);assert.equal(Object.hasOwn(body,"cookies"),false);paired=true;
      res.end(JSON.stringify({project_id:"00000000-0000-4000-8000-000000000002"}));return;
    }
    assert.equal(req.headers["authorization"]?.startsWith("Bearer "),true);
    if(req.url.endsWith("/status")){res.end(JSON.stringify({permission:automatic?{version:1,mode:missingAccount?"cloud_preferred":"local_only"}:null,cloud_available:missingAccount,session_available:missingAccount,session_expires_at:new Date(Date.now()+3*86400000).toISOString()}));return;}
    if(req.url.endsWith("/pending")){res.end(JSON.stringify(job));return;}
    if(req.url.endsWith("/claim")){job.state="collecting";if(handoff){job.cloud_transport="local_backup";job.execution_mode="local";}res.end(JSON.stringify({...job,lease:"synthetic-collection-lease-1234567890123456789"}));return;}
    if(req.url.endsWith("/heartbeat")){res.end(JSON.stringify(job));return;}
    if(req.url.endsWith("/page")){
      pages.push(body);
      if(body.page===1){accepted=1;job.next_page=2;if(failOnce){failOnce=false;res.statusCode=503;res.end(JSON.stringify({detail:"backend_unavailable"}));return;}}
      else {assert.equal(body.page,2);accepted=2;job.state="completed";job.friend_index=1;job.next_page=1;}
      res.end(JSON.stringify(job));return;
    }
    if(req.url.endsWith("/pause")){job.state="paused";job.reason=body.reason;res.end(JSON.stringify(job));return;}
    res.statusCode=404;res.end('{}');
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));t.after(()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve)}));
  const base=`http://127.0.0.1:${server.address().port}`;
  const fixture=join(dir,"extension");await cp(extensionPath,fixture,{recursive:true});
  const workerFile=join(fixture,"src/collection/worker.js");
  let code=await readFile(workerFile,"utf8");code=code.replace("const origins = new Set([",`const origins = new Set([${JSON.stringify(base)},`);await writeFile(workerFile,code);
  const context=await chromium.launchPersistentContext(join(dir,"profile"),{channel:"chromium",headless:true,args:[`--disable-extensions-except=${fixture}`,`--load-extension=${fixture}`]});
  t.after(()=>context.close());
  await context.route("https://www.linkedin.com/**",route=>{const url=new URL(route.request().url());const page=Number(url.searchParams.get("page")||1);return route.fulfill({contentType:"text/html; charset=utf-8",body:url.pathname.startsWith("/in/")?(missingAccount&&job.state==="collecting"?profileHTML().replace(/<header[\s\S]*?<\/header>/, ""):profileHTML()):resultsHTML(page,[person(page)],page===1?"next":"end")});});
  let worker=context.serviceWorkers()[0]||await context.waitForEvent("serviceworker");
  const runtimeErrors=[];worker.on("console",msg=>{if(msg.type()==="error")runtimeErrors.push(msg.text());});
  const li=await context.newPage();await li.goto(selection.friend.profile_url);
  const dashboard=await context.newPage();await dashboard.goto(base);
  const response=await dashboard.evaluate(()=>new Promise(resolve=>{
    setTimeout(()=>resolve({ok:false,error:"pairing_timeout"}),15000);
    window.addEventListener("message",function listener(event){if(event.data?.source==="tin.linkedin.collection.v3"&&event.data.id==="fixture-pair"){window.removeEventListener("message",listener);resolve(event.data);}});
    window.postMessage({source:"tin.dashboard.collection.v3",type:"PAIR",id:"fixture-pair",grant:"synthetic-pairing-grant-12345678901234567890"},location.origin);
  }));
  assert.equal(response.ok,true,JSON.stringify(response));assert.equal(paired,true);assert.equal(JSON.stringify(response).includes("bearer"),false);
  const id=new URL(worker.url()).hostname;
  if(handoff) {
    await worker.evaluate(async job=>{
      const key="tin.linkedin.collection.v3";
      const state=(await chrome.storage.local.get(key))[key];
      await chrome.storage.local.set({[key]:{...state,status:"cloud_running",job}});
      await chrome.alarms.create(key,{when:Date.now()+100});
    },job);
  } else {
  if (!automatic) {
  const popup=await context.newPage();await popup.goto(`chrome-extension://${id}/popup/collection.html`);
  await popup.locator("#begin:enabled").click();
  await popup.waitForFunction(()=>document.querySelector("#status").textContent.includes("Collecting"));
  await popup.close();
  }
  if(missingAccount) {
    const deadline=Date.now()+35000;
    while(Date.now()<deadline && job.state!=="paused")await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(job.state,"paused");assert.equal(job.reason,"browser_unavailable");assert.equal(accepted,0);
    const saved=await worker.evaluate(async()=> (await chrome.storage.local.get("tin.linkedin.collection.v3"))["tin.linkedin.collection.v3"]);
    assert.equal(saved.connection.session_available,true);
    assert.equal(saved.page_diagnostics.account.ok,false);
    assert.equal(saved.page_diagnostics.page_kind,"profile");
    assert.equal(JSON.stringify(saved.page_diagnostics).includes("Test Owner"),false);
    assert.ok(await li.locator(".global-nav__me-profile-link").isVisible(),"the original signed-in page remains intact");
    return;
  }
  const firstPageDeadline=Date.now()+30000;
  while(Date.now()<firstPageDeadline&&accepted<1&&job.state!=="paused")await new Promise(resolve=>setTimeout(resolve,100));
  assert.equal(accepted,1,"first batch was persisted before restart");
  const control=await context.newPage();await control.goto(`chrome-extension://${id}/popup/collection.html`);
  const cdp=await context.newCDPSession(control);
  const versions=new Map();
  cdp.on("ServiceWorker.workerVersionUpdated", event=>{for(const version of event.versions) versions.set(version.versionId,version);});
  await cdp.send("ServiceWorker.enable");
  const versionDeadline=Date.now()+5000;
  while(Date.now()<versionDeadline&&![...versions.values()].some(v=>v.scriptURL===worker.url()))await new Promise(r=>setTimeout(r,50));
  const version=[...versions.values()].find(v=>v.scriptURL===worker.url()&&v.runningStatus==="running");
  assert.ok(version,"Chrome exposes the running extension worker");
  const before=await control.evaluate(async()=> (await chrome.runtime.getContexts({contextTypes:["BACKGROUND"]}))[0].contextId);
  await cdp.send("ServiceWorker.stopWorker",{versionId:version.versionId});
  await control.evaluate(()=>chrome.runtime.sendMessage({namespace:"tin.linkedin.collection.v3",type:"STATUS"}));
  const after=await control.evaluate(async()=> (await chrome.runtime.getContexts({contextTypes:["BACKGROUND"]}))[0].contextId);
  assert.notEqual(before,after,"background context was recreated");
  await control.close();
  }
  const deadline=Date.now()+60000;
  while(Date.now()<deadline&&accepted<2&&job.state!=="paused")await new Promise(resolve=>setTimeout(resolve,250));
  const info={...job};
  if(job.state!=="completed") info.pages=await Promise.all(context.pages().map(async p=>({url:p.url(),text:(await p.locator("body").innerText()).slice(0,300)})));
  assert.equal(job.state,"completed",JSON.stringify(info));
  assert.equal(pages.length,handoff?1:3);
  if(handoff) assert.equal(pages[0].page,2,"handoff retains the first accepted cloud page");
  else assert.deepEqual(pages[0],pages[1]);
  assert.equal(new URL(pages[0].source.collection_url).searchParams.get("network"),'["S"]');
  assert.equal(new URL(pages[0].source.collection_url).searchParams.get("keywords"),'founder');
  assert.equal(runtimeErrors.some(message=>message.includes("chrome-extension://invalid")),false);
  assert.equal(await li.evaluate(()=>typeof globalThis.TinLinkedIn),"undefined");
});
