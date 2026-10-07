import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import {chromium} from "playwright";

for (const named of [true, false]) test(named ? "LinkedIn guides installation, confirms one permission in Tin and remembers it" : "an unnamed paired LinkedIn account remains connected and uses the standard integration details", {timeout:45000}, async () => {
  const assets=path.resolve("src/tin_lite/static");
  const project={id:"project-one",name:"Example project",workspace_id:"workspace",member_count:1};
  let configuration={}, connected=false;
  const calls=[], errors=[];
  const account=named ? {key:"https://www.linkedin.com/in/fixture-owner",profile_url:"https://www.linkedin.com/in/fixture-owner",name:"Test Owner"} : {key:"avatar:/dms/image/v2/FIXTURE",profile_url:null,name:""};
  const integration=()=>({key:"network.linkedin",name:"LinkedIn",badge:"in",description:"Collect visible connections.",unlocks:["Collect connections"],configured:true,connection_id:connected?"connection-one":null,status:connected?"connected":"available",configuration,external_account_label:connected?"":null});
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,"http://localhost");
    const send=value=>{res.setHeader("Content-Type","application/json");res.end(JSON.stringify(value));};
    if(url.pathname==="/integrations"){res.setHeader("Content-Type","text/html");return res.end((await fs.readFile(path.join(assets,"index.html"),"utf8")).replaceAll("{{ASSET_VERSION}}","test").replaceAll("{{CLERK_PUBLISHABLE_KEY}}","").replaceAll("{{BILLING_ENABLED}}","false"));}
    if(url.pathname.startsWith("/assets/")){
      try{const file=path.join(assets,url.pathname.slice(8));res.setHeader("Content-Type",file.endsWith(".js")?"text/javascript":file.endsWith(".css")?"text/css":"application/octet-stream");return res.end(await fs.readFile(file));}catch{res.writeHead(404).end();return;}
    }
    if(["POST","PUT"].includes(req.method)){
      let raw="";for await(const c of req)raw+=c;const body=JSON.parse(raw);calls.push({path:url.pathname,body});
      assert.equal(body.consent_version,1);assert.deepEqual(body.actor,account);
      configuration={actor:{...account,name:""},collection_permission:{version:1,mode:body.mode},...(body.mode!=="local_only"?{session_state:"available",session_expires_at:new Date(Date.now()+86400000).toISOString()}: {})};connected=true;
      return send(url.pathname.endsWith("/pairing")?{grant:"synthetic-one-use-grant",project_id:project.id}:{permission:configuration.collection_permission});
    }
    if(url.pathname==="/api/projects")return send([project]);
    if(url.pathname.endsWith("/integrations"))return send([integration()]);
    if(url.pathname.endsWith("/system"))return send({workflow_count:0,running_count:0,waiting_count:0,runs_this_month:0});
    if(url.pathname.startsWith("/api/"))return send([]);
    res.writeHead(404).end();
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true});
  try{
    const context=await browser.newContext({viewport:{width:1280,height:1000}});
    await context.route("**/*",route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await context.addInitScript(({account})=>{
      window.Clerk={load:async()=>{},isSignedIn:true,user:{id:"member"},session:{getToken:async()=>"synthetic"}};
      window.extensionAvailable=false;window.linkedInSignedIn=false;window.extensionProject=null;window.extensionMessages=[];
      window.addEventListener("message",event=>{
        const req=event.data;if(req?.source!=="tin.dashboard.collection.v3"||!window.extensionAvailable)return;
        window.extensionMessages.push(req.type);
        let payload={};
        if(req.type==="DISCOVER")payload={version:"0.4.1",protocol:4,project_id:window.extensionProject,account:window.linkedInSignedIn?account:null,reason:window.setupFailure || (!window.linkedInSignedIn?"open_linkedin_tab":""),cloud_available:true,session_available:window.cloudReady};
        if(req.type==="PAIR"){window.extensionProject="project-one";payload={connected:true,project_id:"project-one"};}
        if(req.type==="WAKE")setTimeout(()=>{if(!window.setupFailure)window.cloudReady=true;},500);
        window.postMessage({source:"tin.linkedin.collection.v3",id:req.id,ok:true,payload},location.origin);
      });
    },{account});
    const page=await context.newPage();page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/integrations?project=project-one`);
    await page.locator('[data-integration-connect="network.linkedin"]').click();
    const dialog=page.getByRole("dialog");
    await dialog.getByText("Install or update the Tin extension, then refresh this Tin tab.").waitFor();
    assert.equal(await dialog.getByRole("link",{name:"Install Tin for Chrome"}).count(),1);
    assert.equal(await dialog.getByRole("button",{name:"Connect",exact:true}).isDisabled(),true);
    assert.equal(calls.length,0);
    if(process.env.TIN_LINKEDIN_INSTALL_SCREENSHOT)await dialog.screenshot({path:process.env.TIN_LINKEDIN_INSTALL_SCREENSHOT});
    await page.evaluate(()=>{window.extensionAvailable=true;});
    await dialog.getByRole("button",{name:"Check again"}).click();
    await dialog.getByText("Open LinkedIn and sign in, then check again.").waitFor();
    await page.evaluate(()=>{window.linkedInSignedIn=true;});
    await dialog.getByRole("button",{name:"Check again"}).click();
    await dialog.getByText(`Account: ${named ? "Test Owner" : "your signed-in LinkedIn account"}`).waitFor();
    assert.equal(await dialog.getByRole("radio",{name:"Cloud with browser backup",exact:true}).isChecked(),true);
    assert.match(await dialog.innerText(),/securely stores your login for up to 7 days/);
    for(const width of [1280,390]){
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    }
    if(process.env.TIN_LINKEDIN_SETUP_SCREENSHOT)await dialog.screenshot({path:process.env.TIN_LINKEDIN_SETUP_SCREENSHOT});
    await dialog.getByRole("button",{name:"Connect",exact:true}).click();
    await dialog.waitFor({state:"hidden"});
    assert.equal(calls.length,1);assert.equal(calls[0].body.mode,"cloud_preferred");
    assert.deepEqual((await page.evaluate(()=>window.extensionMessages)).filter(t=>t!=="DISCOVER"),["PAIR","WAKE"]);
    await page.getByRole("button",{name:"Configure",exact:true}).click();
    assert.equal(await page.getByText("Choose an account",{exact:true}).count(),0);
    if(!named)assert.equal(await page.locator(".integration-primary").textContent(),"LinkedIn account connected");
    assert.equal(await page.locator(".integration-expanded .integration-detail-row").count(),4);
    assert.equal(await page.locator(".integration-control-footer").count(),1);
    assert.equal(await page.getByRole("button",{name:"Done",exact:true}).count(),1);
    if(process.env.TIN_LINKEDIN_CARD_SCREENSHOT)await page.locator(".integration-card.is-connected").screenshot({path:process.env.TIN_LINKEDIN_CARD_SCREENSHOT});
    await page.getByRole("button",{name:"Settings",exact:true}).click();
    await dialog.getByText(`Account: ${named ? "Test Owner" : "your signed-in LinkedIn account"}`).waitFor();
    assert.equal(await dialog.getByRole("radio",{name:"Cloud with browser backup",exact:true}).isChecked(),true);
    await dialog.getByRole("radio",{name:"This browser only",exact:true}).check();
    assert.match(await dialog.innerText(),/without uploading your login/);
    await dialog.getByRole("button",{name:"Save connection",exact:true}).click();
    await dialog.waitFor({state:"hidden"});
    assert.equal(calls[1].path,"/api/projects/project-one/connection-collection/preferences");
    assert.equal(calls[1].body.mode,"local_only");
    assert.equal((await page.evaluate(()=>window.extensionMessages)).filter(t=>t==="PAIR").length,1);
    await page.getByRole("button",{name:"Configure",exact:true}).click();
    await page.getByRole("button",{name:"Settings",exact:true}).click();
    await dialog.getByRole("radio",{name:"Cloud with browser backup",exact:true}).check();
    await page.evaluate(()=>{window.cloudReady=false;window.setupFailure="search_setup_unavailable";});
    await dialog.getByRole("button",{name:"Save connection",exact:true}).click();
    await dialog.getByText("Tin could not prepare LinkedIn search. Keep LinkedIn open and try again.").waitFor();
    assert.equal(await dialog.isVisible(),true,"a saved preference is not reported as finished cloud setup");
    assert.equal(await dialog.getByRole("button",{name:"Try again",exact:true}).isEnabled(),true);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
});
