import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync, realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import http from 'node:http';
import net from 'node:net';
import dgram from 'node:dgram';
import {chromium} from 'playwright';
import {createFilmPolicy, installFilmRoutes, launchFilmBrowser} from './lib/browser-safety.mjs';

test('launch browser reads only public film assets and never reaches a server', async () => {
  const root=realpathSync(mkdtempSync(join(tmpdir(),'tin-launch-guard-')));
  const film=join(root,'film');mkdirSync(film);mkdirSync(join(film,'private'));
  const secret='FAKE_TEST_CANARY_NOT_A_REAL_KEY';
  writeFileSync(join(root,'outside.svg'),secret);
  writeFileSync(join(film,'.env'),secret);
  writeFileSync(join(film,'private','notes.json'),secret);
  symlinkSync(join(root,'outside.svg'),join(film,'escape.svg'));
  writeFileSync(join(film,'visible.json'),'{"product":"Synthetic product"}');
  writeFileSync(join(film,'mark.svg'),'<svg xmlns="http://www.w3.org/2000/svg"/>');
  let hits=0;const server=http.createServer((req,res)=>{hits++;res.end('unexpected');});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const address=`http://127.0.0.1:${server.address().port}/collect`;
  const pageFile=join(film,'index.html');
  writeFileSync(pageFile,`<script>
    window.checked=Promise.all(['visible.json','.env','private/notes.json','escape.svg','../outside.svg'].map(async p=>{
      const r=await fetch(p);return {status:r.status,text:await r.text()};
    }));fetch(${JSON.stringify(address)}).catch(()=>{});new Image().src=${JSON.stringify(address)};
  </script>`);
  let browser;
  try {
    const policy=createFilmPolicy(pageFile);
    for(const file of ['.env','private/notes.json','escape.svg'])assert.throws(()=>policy.asset(join(film,file)));
    assert.throws(()=>policy.asset(join(root,'outside.svg')));
    assert.throws(()=>policy.asset(join(film,'visible.json'),{imageOnly:true}));
    assert.equal(policy.asset(join(film,'mark.svg'),{imageOnly:true}),join(film,'mark.svg'));
    browser=await launchFilmBrowser(chromium);
    const context=await browser.newContext({serviceWorkers:'block',acceptDownloads:false});
    await installFilmRoutes(context,policy);
    const page=await context.newPage();await page.goto(policy.url().href);
    const rows=await page.evaluate(()=>window.checked);
    assert.equal(rows[0].status,200);
    assert(rows.slice(1).every(r=>r.status===403&&!r.text.includes(secret)));
    assert.deepEqual(await page.evaluate(() => {
      const frame=document.createElement('iframe');document.body.append(frame);
      return [typeof window.RTCPeerConnection, typeof frame.contentWindow.RTCPeerConnection];
    }), ['undefined','undefined']);
    await page.evaluate(()=>{const frame=document.createElement('iframe');frame.src='visible.json';document.body.append(frame);});
    await page.waitForTimeout(50);
    assert.equal(hits,0);
  } finally {
    if(browser)await browser.close();
    await new Promise(r=>server.close(r));
    rmSync(root,{recursive:true,force:true});
  }
});

for (const transport of ['udp','tcp']) test(`launch proxy blocks native WebRTC ${transport} even without JavaScript guards`, async () => {
  let received=0,browser;
  const sockets=new Set();
  const server=transport==='udp'?dgram.createSocket('udp4'):net.createServer(socket=>{
    sockets.add(socket);received++;socket.on('data',()=>received++);socket.on('close',()=>sockets.delete(socket));
  });
  if(transport==='udp')server.on('message',()=>received++);
  await new Promise(r=>transport==='udp'?server.bind(0,'127.0.0.1',r):server.listen(0,'127.0.0.1',r));
  try {
    browser=await launchFilmBrowser(chromium);
    const page=await browser.newPage();await page.setContent('<body>Synthetic guard test</body>');
    const url=transport==='udp'?`stun:127.0.0.1:${server.address().port}`:`turn:127.0.0.1:${server.address().port}?transport=tcp`;
    await page.evaluate(async url=>{
      const peer=new RTCPeerConnection({iceServers:[{urls:url,username:'fake-user',credential:'fake-password'}]});
      window.peer=peer;peer.createDataChannel('test');await peer.setLocalDescription(await peer.createOffer());
    },url);
    await page.waitForTimeout(1500);await page.evaluate(()=>window.peer.close());
    assert.equal(received,0);
  } finally {
    if(browser)await browser.close();
    for(const socket of sockets)socket.destroy();
    await new Promise(r=>server.close(r));
  }
});
