import assert from "node:assert/strict";
import test from "node:test";
import {mkdtemp,cp,readFile,writeFile,rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createServer} from "node:http";
import {chromium} from "playwright";
import {selection,profileHTML} from "./fixtures.mjs";

test("MV3 prepares a reusable session once, keeps cookies local and honors saved permission", {timeout:45000}, async t=>{
  const dir=await mkdtemp(join(tmpdir(),"tin-setup-mv3-"));t.after(()=>rm(dir,{recursive:true,force:true}));
  const key="tin.linkedin.collection.v3", project="00000000-0000-4000-8000-000000000002";
  let transfers=0,checks=0;
  const status={permission:{version:1,mode:"local_only"},cloud_available:true,session_available:false,session_generation:null};
  const server=createServer(async(req,res)=>{
    res.setHeader("Content-Type","application/json");res.setHeader("Access-Control-Allow-Origin","*");
    if(req.url==="/"){res.setHeader("Content-Type","text/html");res.end("<!doctype html><title>Tin setup</title>");return;}
    assert.equal(req.headers.authorization,"Bearer synthetic-device");
    if(req.url.endsWith("/status")){checks++;res.end(JSON.stringify(status));return;}
    if(req.url.endsWith("/pending")){res.end("null");return;}
    if(req.url.endsWith("/session")){
      let raw="";for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw);
      assert.equal(status.permission.mode,"cloud_preferred");
      assert.equal(body.actor_key,selection.actor.key);
      assert.equal(body.expected_generation,null);
      assert.match(body.query_id,/^voyagerSearchDashClusters\./);
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
  await context.route("https://www.linkedin.com/**",r=>r.fulfill({contentType:"text/html",body:profileHTML()}));
  await context.addCookies(["li_at","JSESSIONID","unrelated"].map(name=>({name,value:`fixture-${name}`,domain:".linkedin.com",path:"/",secure:true,httpOnly:true,expires:Date.now()/1000+3*86400})));
  const li=await context.newPage();await li.goto(selection.friend.profile_url);
  const worker=context.serviceWorkers()[0]||await context.waitForEvent("serviceworker");
  await worker.evaluate(async({key,base,project,actor})=>{
    const tab=(await chrome.tabs.query({url:"https://www.linkedin.com/*"}))[0];
    // Request-observer output is synthetic; Chrome cookie access and upload are real.
    await chrome.storage.local.set({
      [key]:{base,project_id:project,bearer:"synthetic-device",actor,status:"ready"},
      "tin.linkedin.collection.context.v3":{at:Date.now(),tab_id:tab.id,context:{accept_language:"en-US",li_lang:"en_US",li_track:{clientVersion:"fixture-v1",osName:"Mac OS",timezoneOffset:0,timezone:"UTC",deviceFormFactor:"DESKTOP",mpName:"voyager-web"}}},
      "tin.linkedin.collection.search":{id:"voyagerSearchDashClusters."+"a".repeat(32),at:Date.now(),tab_id:tab.id,client_version:"fixture-v1"}
    });
    await chrome.alarms.create(key,{when:Date.now()+100});
  },{key,base,project,actor:selection.actor});
  const wait=async predicate=>{const until=Date.now()+15000;while(!predicate()){assert.ok(Date.now()<until,"worker did not finish");await new Promise(r=>setTimeout(r,100));}};
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
});
