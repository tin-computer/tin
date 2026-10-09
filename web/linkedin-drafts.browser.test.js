import assert from 'node:assert/strict';
import test from 'node:test';
import {chromium} from 'playwright';
import {serveApp,openApp,revision} from './app-fixture.js';
const path='social/linkedin/drafts/2026-10-08-example.json';
const draft={schema_version:'tin.social.linkedin_draft.v1',batch_id:'example',author:'Ada',audience:'Founders building their first product.',style_path:'',brand_path:'brand/BRAND.md',template_path:'formats.json',sources:[],parent:null,gaps:[],posts:[
 {id:'p1',text:'A useful lesson.\n\n<script>not markup</script>',angle:'Shared context',template_id:'lesson',readiness:'ready',support:[],editor_notes:'Private editorial note.'},
 {id:'p2',text:'Another point worth sharing.',angle:'Working together',template_id:'lesson',readiness:'needs_evidence',support:[],editor_notes:'Add evidence.'},
]};
test('LinkedIn drafts preserve literal text, edits and conflicts without publishing',async()=>{
 const {server,base}=await serveApp();const browser=await chromium.launch({headless:true});
 try{
  const {page,context,errors}=await openApp(browser,base,{url:'/files'});
  await context.grantPermissions(['clipboard-read','clipboard-write']);
  let saved=structuredClone(draft), conflict=false;const writes=[], revisions=[];
  await page.route('**/api/projects/project/linkedin/drafts**',async route=>{
   const request=route.request();
   if(new URL(request.url()).pathname.endsWith('/revisions')){revisions.push(request.postDataJSON());return route.fulfill({status:202,json:{id:'revision-run'}});}
   if(request.method()==='PUT'){
    writes.push(request.postDataJSON());
    if(conflict)return route.fulfill({status:409,json:{detail:'This file changed. Reload the saved version.'}});
    saved=request.postDataJSON().draft;
   }
   return route.fulfill({json:{path,revision,draft:saved,sha256:'a'.repeat(64),revision_source_run_id:'source-run'}});
  });
  await page.route('**/api/workflows/runs/revision-run',route=>route.fulfill({json:{id:'revision-run',status:'succeeded',artifact_path:'social/linkedin/drafts/revised.json'}}));
  await page.goto(`${base}/files?${new URLSearchParams({project:'project',linkedin_draft:path})}`);
  await page.locator('[data-linkedin-copy]').waitFor();
  assert.equal(await page.locator('[data-linkedin-copy]').textContent(),draft.posts[0].text);
  assert.equal(await page.locator('[data-linkedin-copy] script').count(),0);
  assert.equal(await page.getByText('Private editorial note.').count(),0);
  assert.equal(await page.getByRole('button',{name:/Publish|Preview post/}).count(),0);
  await page.getByRole('button',{name:'Copy post',exact:true}).click();
  assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),draft.posts[0].text);
  await page.getByRole('button',{name:'Draft notes',exact:true}).click();
  await page.getByText('Private editorial note.').waitFor();
  await page.getByRole('button',{name:'Edit post',exact:true}).click();
  await page.getByLabel('Post text',{exact:true}).fill('Edited plain text.');
  await page.getByRole('button',{name:'Post 2',exact:false}).click();
  assert.equal(await page.locator('[data-linkedin-copy]').textContent(),draft.posts[1].text);
  await page.getByRole('button',{name:'Post 1',exact:false}).click();
  assert.equal(await page.getByLabel('Post text',{exact:true}).inputValue(),'Edited plain text.');
  await page.locator('.nav-item[data-view="integrations"]').click();
  await page.goBack();await page.getByText('Your unsaved edits are still here.').waitFor();
  await page.getByRole('button',{name:'Edit post',exact:true}).click();
  await page.getByRole('button',{name:'Save draft',exact:true}).click();await page.getByText('Draft saved.',{exact:true}).waitFor();
  assert.equal(writes[0].expected_revision,revision);
  assert.equal(saved.posts[0].readiness,'edited');assert.deepEqual(saved.posts[1],draft.posts[1]);
  await page.getByRole('button',{name:'Request changes',exact:true}).click();
  await page.getByLabel('What should change?',{exact:true}).fill('Make the point more direct.');
  await page.getByRole('button',{name:'Revise this post',exact:true}).click();
  await page.getByRole('button',{name:'Open revised batch',exact:true}).waitFor();
  assert.equal(revisions.length,1);assert.equal(revisions[0].post_id,'p1');
  assert.equal(revisions[0].expected_sha256,'a'.repeat(64));
  assert.equal(await page.locator('[data-linkedin-copy]').textContent(),'Edited plain text.');
  conflict=true;await page.getByLabel('Post text',{exact:true}).fill('Keep this newer edit.');
  await page.getByRole('button',{name:'Save draft',exact:true}).click();
  await page.getByText(/Your edits are still here. Copy them before reloading./).waitFor();
  assert.equal(await page.getByLabel('Post text',{exact:true}).inputValue(),'Keep this newer edit.');
  for(const width of [1280,390]){
   await page.setViewportSize({width,height:1000});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   if(process.env.TIN_LINKEDIN_DRAFT_SCREENSHOTS)await page.screenshot({path:`${process.env.TIN_LINKEDIN_DRAFT_SCREENSHOTS}-${width}.png`,fullPage:true});
  }
  assert.deepEqual(errors,[]);
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
});
