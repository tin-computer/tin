import assert from "node:assert/strict";
import test from "node:test";
import {mkdtemp,cp,readFile,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createServer} from "node:http";
import {chromium} from "playwright";
import {selection,profileHTML} from "./fixtures.mjs";

for (const mode of ["stored", "embedded", "unavailable"]) test(mode === "embedded" ? "fresh MV3 setup reads embedded search metadata without a search request or seeded query" : mode === "unavailable" ? "setup verifies the login when the initial search has no query and leaves friend preparation to the run" : "MV3 prepares a reusable session once, keeps cookies local and honors saved permission", {timeout:65000}, async t=>{
  const embedded = mode !== "stored";
  const dir=await mkdtemp(join(tmpdir(),"tin-setup-mv3-"));t.after(()=>rm(dir,{recursive:true,force:true}));
  const key="tin.linkedin.collection.v3", project="00000000-0000-4000-8000-000000000002";
  let transfers=0,checks=0,rejections=0,rejectRefresh=false;
  const status={permission:{version:1,mode:"local_only"},cloud_available:true,session_available:false,session_generation:null};
  const server=createServer(async(req,res)=>{
    res.setHeader("Content-Type","application/json");res.setHeader("Access-Control-Allow-Origin","*");
    if(req.url==="/"){res.setHeader("Content-Type","text/html");res.end("<!doctype html><title>Tin setup</title>");return;}
    assert.equal(req.headers.authorization,"Bearer synthetic-device");
    if(req.url.endsWith("/status")){checks++;res.end(JSON.stringify(status));return;}
    if(req.url.endsWith("/pending")){res.end("null");return;}
    if(req.url.endsWith("/session")){
      let raw="";for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
      if(rejectRefresh){rejections++;res.writeHead(409).end(JSON.stringify({detail:"challenge"}));return;}
      assert.equal(status.permission.mode,"cloud_preferred");
      assert.equal(body.actor_key,selection.actor.key);
      assert.equal(body.expected_generation,null);
      if(mode === "unavailable")assert.equal(body.query_id,null);else assert.match(body.query_id,/^voyagerSearchDashClusters\./);
      assert.deepEqual(body.session.cookies.map(c=>c.name),["JSESSIONID","li_at"]);
      assert.equal(body.session.browser_context.li_track.clientVersion,"fixture-v1");
      assert.match(body.session.user_agent,/Chrome/);
      transfers++;
      Object.assign(status,{session_available:true,session_generation:"generation-one",session_expires_at:new Date(Date.now()+3*86400000).toISOString()});
      res.end(JSON.stringify(status));return;
    }
    res.writeHead(404).end("{}");
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));t.after(()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);}));
  const base=`http://127.0.0.1:${server.address().port}`;
  const extension=join(dir,"extension");await cp(new URL("../..",import.meta.url).pathname,extension,{recursive:true});
  const workerFile=join(extension,"src/collection/worker.js");
  await writeFile(workerFile,(await readFile(workerFile,"utf8")).replace("const origins = new Set([",`const origins = new Set([${JSON.stringify(base)},`));
  const context=await chromium.launchPersistentContext(join(dir,"profile"),{channel:"chromium",headless:true,args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});t.after(()=>context.close());
  const requested=[];
  let worker;
  await context.route("https://www.linkedin.com/**",async r=>{
    const url = new URL(r.request().url());
    requested.push(url.pathname);
    if(url.pathname.startsWith("/voyager/")) {
      assert.equal(url.searchParams.has("queryId"),false,"no live search request is needed for embedded results");
      // Playwright-fulfilled requests never reach Chrome's header observer. Supply
      // only that observer's normalized context; the search identifier is unseeded.
      await worker.evaluate(async url=>{
        const tab=(await chrome.tabs.query({url}))[0];
        const observed={at:Date.now(),tab_id:tab.id,context:{accept_language:"en-US",li_lang:"en_US",li_track:{clientVersion:"fixture-v1",osName:"Mac OS",timezoneOffset:0,timezone:"UTC",deviceFormFactor:"DESKTOP",mpName:"voyager-web"}}};
        await chrome.storage.local.set({[`tin.linkedin.collection.context.${tab.id}`]:observed,"tin.linkedin.collection.context.v3":{...observed,tab_id:999999}});
      },r.request().frame().url());
      return r.fulfill({contentType:"application/json",body:"{}"});
    }
    const track={clientVersion:"fixture-v1",osName:"Mac OS",timezoneOffset:0,timezone:"UTC",deviceFormFactor:"DESKTOP",mpName:"voyager-web"};
    const boot=embedded
      ? `${mode !== "unavailable" && url.pathname.startsWith("/search/")?`<code id="bpr-guid-fixture" data-request="/voyager/api/graphql?queryId=voyagerSearchDashClusters.${"a".repeat(32)}" hidden>{"data":"synthetic result data stays in the tab"}</code>`:""}<script>fetch('/voyager/api/me',{headers:{'accept-language':'en-US','x-li-lang':'en_US','x-li-track':${JSON.stringify(JSON.stringify(track))}}})</script>` : "";
    return r.fulfill({contentType:"text/html",body:profileHTML()+boot});
  });
  await context.addCookies(["li_at","JSESSIONID","unrelated"].map(name=>({name,value:`fixture-${name}`,domain:".linkedin.com",path:"/",secure:true,httpOnly:true,expires:Date.now()/1000+3*86400})));
  worker=context.serviceWorkers()[0]||await context.waitForEvent("serviceworker");
  const li=await context.newPage();await li.goto(selection.friend.profile_url);
  await worker.evaluate(async({key,base,project,actor,embedded})=>{
    const tab=(await chrome.tabs.query({url:"https://www.linkedin.com/*"}))[0];
    // Request-observer output is synthetic; Chrome cookie access and upload are real.
    await chrome.storage.local.set({
      [key]:{base,project_id:project,bearer:"synthetic-device",actor,status:"ready"},
      "tin.linkedin.collection.context.v3":{at:Date.now(),tab_id:tab.id,context:{accept_language:"en-US",li_lang:"en_US",li_track:{clientVersion:"fixture-v1",osName:"Mac OS",timezoneOffset:0,timezone:"UTC",deviceFormFactor:"DESKTOP",mpName:"voyager-web"}}},
      ...(!embedded?{"tin.linkedin.collection.search":{id:"voyagerSearchDashClusters."+"a".repeat(32),at:Date.now(),tab_id:tab.id,client_version:"fixture-v1"}}:{})
    });
    await chrome.alarms.create(key,{when:Date.now()+100});
  },{key,base,project,actor:selection.actor,embedded});
  const wait=async predicate=>{const until=Date.now()+22000;while(!predicate()){
    if(Date.now()>=until){const state=await worker.evaluate(async key=>{const s=(await chrome.storage.local.get(key))[key];return {status:s.status,reason:s.reason,keys:Object.keys(await chrome.storage.local.get(null))};},key);assert.fail(`worker did not finish: ${JSON.stringify({state,requested})}`);}
    await new Promise(r=>setTimeout(r,100));}};
  await wait(()=>checks>0);assert.equal(transfers,0);
  status.permission.mode="cloud_preferred";
  await worker.evaluate(key=>chrome.alarms.create(key,{when:Date.now()+100}),key);
  await wait(()=>transfers===1);
  const before=checks;
  await worker.evaluate(key=>chrome.alarms.create(key,{when:Date.now()+100}),key);
  await wait(()=>checks>before);
  assert.equal(transfers,1,"later polls reuse the encrypted server session");
  assert.equal((await context.cookies("https://www.linkedin.com")).find(c=>c.name==="li_at").value,"fixture-li_at");
  const storage=await worker.evaluate(()=>chrome.storage.local.get(null));
  assert.equal(JSON.stringify(storage).includes("fixture-li_at"),false,"raw session is not persisted in extension state");
  assert.equal(context.pages().length,2,"setup with current context does not create a popup");
  rejectRefresh=true;status.session_available=false;
  await worker.evaluate(key=>chrome.alarms.create(key,{when:Date.now()+100}),key);
  await wait(()=>rejections===1);
  const checked=checks;
  await worker.evaluate(key=>chrome.alarms.create(key,{when:Date.now()+100}),key);
  await wait(()=>checks>checked);
  assert.equal(rejections,1,"a challenge waits for the user instead of repeating transfers");
  assert.equal((await worker.evaluate(key=>chrome.storage.local.get(key),key))[key].refresh_blocked,true);
});
