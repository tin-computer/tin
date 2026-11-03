// Render a seek(t) film page to frames and video.
//
//   node tools/render.mjs video  --page src/index.html --out out/draft.mp4 [--fps 60 --sub 4 --shutter 270 --w 1920 --h 1080 --from 0 --to <dur> --workers 4]
//   node tools/render.mjs stills --page src/index.html --at 0.5,2.1,7.87 --outdir out/stills [--w 1920 --h 1080]
//
// The page must set window.FILM = { duration, width, height } and window.seek = (t) => void|Promise.
// seek(t) must paint the exact frame for film time t from nothing but t: no timers, no CSS transitions,
// no requestAnimationFrame, no state carried between calls, no Math.random.
// --sub N renders N subframes per output frame and averages them (motion blur). 1 = off.
// --shutter D spreads the subframes over D degrees of the frame interval, ending on the frame time (default 270,
// the original behaviour). 180 halves the gap between subframes, so fast moves smear instead of stepping.
// The page is told fps, sub and shutter in its query string, so it can add matching analytic blur of its own.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { resolve, join } from 'node:path';
import {createFilmPolicy, installFilmRoutes, launchFilmBrowser} from './lib/browser-safety.mjs';

const FF = process.env.FFMPEG || 'ffmpeg';
const mode = process.argv[2];
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > 0 ? process.argv[i + 1] : d; };
const page_ = resolve(arg('page', 'src/index.html'));
const W = Number(arg('w', 1920)), H = Number(arg('h', 1080));
const SHUTTER = Number(arg('shutter', 270));
if (![W, H].every(n => Number.isInteger(n) && n > 0 && n <= 1920)) throw new Error('Use integer width/height between 1 and 1920');
const filmPolicy = createFilmPolicy(page_);

async function open(browser, q = '') {
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, serviceWorkers: 'block', acceptDownloads: false });
  await installFilmRoutes(ctx, filmPolicy, issue => console.error('blocked film request:', JSON.stringify(issue)));
  const page = await ctx.newPage();
  page.on('pageerror', e => console.error('page error:', e.message));
  await page.goto(filmPolicy.url(`render=1&w=${W}&h=${H}${q}`).href);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(() => typeof window.seek === 'function' && window.FILM);
  return page;
}
const shoot = async (page, t, type = 'jpeg') => {
  await page.evaluate(async (t) => { await window.seek(t); }, t);
  return page.screenshot({ type, ...(type === 'jpeg' ? { quality: 95 } : {}), clip: { x: 0, y: 0, width: W, height: H } });
};

let browser;
const encoders = new Set();
try {
if (mode === 'stills') {
  const outdir = resolve(arg('outdir', 'out/stills')); mkdirSync(outdir, { recursive: true });
  const ats = arg('at', '0').split(',').map(Number);
  browser = await launchFilmBrowser(chromium);
  const page = await open(browser);
  for (const t of ats) {
    const png = await shoot(page, t, 'png');
    const name = join(outdir, `t${t.toFixed(3).padStart(8, '0')}.png`);
    writeFileSync(name, png); console.log(name);
  }
} else if (mode === 'video') {
  const FPS = Number(arg('fps', 60)), SUB = Number(arg('sub', 4)), WORKERS = Number(arg('workers', 4));
  const out = resolve(arg('out', 'out/draft.mp4'));
  browser = await launchFilmBrowser(chromium);
  const probe = await open(browser);
  const dur = await probe.evaluate(() => window.FILM.duration);
  if (!Number.isFinite(dur) || dur < 8 || dur > 90) throw new Error('Launch duration must be 8–90 seconds');
  await probe.context().close();
  const from = Number(arg('from', 0)), to = Number(arg('to', dur));
  if (![FPS, SUB, WORKERS].every(n => Number.isInteger(n) && n > 0) || FPS > 60 || SUB > 4 || WORKERS > 4 || !Number.isFinite(from) || !Number.isFinite(to) || from < 0 || to > dur || from >= to) throw new Error('Invalid bounded launch render settings');
  const total = Math.round((to - from) * FPS);
  const tmp = out + '.parts'; rmSync(tmp, { recursive: true, force: true }); mkdirSync(tmp, { recursive: true });
  const per = Math.ceil(total / WORKERS);
  const t0 = Date.now();
  let done = 0;
  async function segment(w) {
    const a = w * per, b = Math.min(total, a + per);
    if (a >= b) return null;
    const page = await open(browser, `&fps=${FPS}&sub=${SUB}&shutter=${SHUTTER}`);
    const file = join(tmp, `part${String(w).padStart(2, '0')}.mp4`);
    const vf = SUB > 1 ? `tmix=frames=${SUB}:weights='${Array(SUB).fill(1).join(' ')}',select='eq(mod(n\\,${SUB})\\,${SUB - 1})',setpts=N/${FPS}/TB` : 'null';
    const ff = spawn(FF, ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS * SUB), '-i', '-',
      '-vf', vf, '-r', String(FPS), '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p', file],
      { stdio: ['pipe', 'inherit', 'inherit'] });
    encoders.add(ff);ff.once('close', () => encoders.delete(ff));
    const encoded = new Promise((r, j) => { ff.on('error', j); ff.on('close', c => c ? j(new Error('Frame encoding failed')) : r()); });
    encoded.catch(() => {});
    ff.stdin.on('error', () => {});
    for (let f = a; f < b; f++) {
      for (let s = 0; s < SUB; s++) {
        // subframes spread across `shutter` degrees of the frame interval, ending on the frame time
        // (shutter 270 with SUB 4 reproduces the original offsets -3/4, -2/4, -1/4, 0 of a frame)
        const span = SUB > 1 ? (SHUTTER / 360) * (SUB - 1 - s) / (SUB - 1) : 0;
        const t = from + (f - span) / FPS;
        const img = await shoot(page, Math.max(from, t));
        if (!ff.stdin.write(img)) await Promise.race([once(ff.stdin, 'drain'), encoded.then(() => { throw new Error('Encoder stopped before the last frame'); })]);
      }
      done++;
      if (done % (FPS * 2) === 0) {
        const el = (Date.now() - t0) / 1000;
        console.log(`frames ${done}/${total}  ${(done / FPS).toFixed(1)}s of ${(total / FPS).toFixed(1)}s  elapsed ${el.toFixed(0)}s  eta ${(el / done * (total - done)).toFixed(0)}s`);
      }
    }
    ff.stdin.end();
    await encoded;
    await page.context().close();
    return file;
  }
  const parts = (await Promise.all(Array.from({ length: WORKERS }, (_, w) => segment(w)))).filter(Boolean);
  writeFileSync(join(tmp, 'list.txt'), parts.map(p => `file '${p}'`).join('\n'));
  await new Promise((r, j) => spawn(FF, ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', join(tmp, 'list.txt'), '-c', 'copy', out], { stdio: 'inherit' }).on('close', c => c ? j(new Error('concat failed')) : r()));
  rmSync(tmp, { recursive: true, force: true });
  console.log(`wrote ${out} (${total} frames, ${((Date.now() - t0) / 1000).toFixed(0)}s)`);
} else {
  console.error('usage: node tools/render.mjs video|stills ...'); process.exit(2);
}
} finally {
  for (const child of encoders) child.kill();
  if (browser) await browser.close();
}
