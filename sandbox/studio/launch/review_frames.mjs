// node review_frames.mjs --page films/launch/src/index.html --w 1280 --h 720 --fps 60 --outdir films/launch/out/frame-review
// FILM.events is the source of all transition times; exactly one event has role: 'result'.
// data-film-review-overflow opts a text container into strict overflow checks.
// data-film-review-ignore="reason" excludes a subtree, with counted exclusions in report.json.
// Unmarked clipping needs visual review. Rotated/3D text, occlusion, canvas, SVG text and
// iframe text are not proved by DOM rectangles. No automatic text-overlap claim is made.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createFilmPolicy, installFilmRoutes, launchFilmBrowser } from './lib/browser-safety.mjs';

const require = createRequire(import.meta.url);

// Runs inside the page after seek(t); only plain, axis-aligned DOM text has reliable bounds.
function inspectText() {
  const findings = [], exclusions = [], coverage = { visible: 0, offscreen_marked: 0, checked: 0, ignored: 0, uncertain: 0, marked: 0 };
  const bodyStyle = getComputedStyle(document.body);
  const staticBody = bodyStyle.position === 'static' && bodyStyle.contain === 'none' &&
    bodyStyle.willChange === 'auto' && bodyStyle.contentVisibility === 'visible' && bodyStyle.containerType === 'normal' &&
    ['transform', 'translate', 'rotate', 'scale', 'filter', 'backdropFilter', 'perspective'].every(key => bodyStyle[key] === 'none');
  const name = el => {
    const parts = [];
    for (let p = el; p; p = p.parentElement) {
      if (p.id) { parts.unshift(`#${p.id}`); break; }
      parts.unshift(`${p.tagName.toLowerCase()}:nth-child(${p.parentElement ? [...p.parentElement.children].indexOf(p) + 1 : 1})`);
    }
    return parts.join(' > ');
  };
  const visible = el => {
    if (getComputedStyle(el).visibility !== 'visible') return false;
    let opacity = 1;
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.display === 'none') return false;
      opacity *= Number(s.opacity);
    }
    return opacity > .01;
  };
  const reliable = el => {
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p), matrix = new DOMMatrixReadOnly(s.transform);
      if (!matrix.is2D || Math.abs(matrix.b) > .0001 || Math.abs(matrix.c) > .0001 || s.perspective !== 'none') return false;
    }
    return true;
  };
  for (const el of document.querySelectorAll('[data-film-review-ignore]')) {
    if (!el.getAttribute('data-film-review-ignore').trim()) findings.push({ level: 'error', code: 'empty_ignore_reason', selector: name(el) });
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.textContent.trim() || !el || el.closest('script,style,noscript,template,svg') || !visible(el)) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
    if (!rects.length) continue;
    const onScreen = rects.some(r => r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight);
    if (!onScreen && !el.closest('[data-film-review-overflow]')) continue;
    if (onScreen) coverage.visible++; else coverage.offscreen_marked++;
    const ignored = el.closest('[data-film-review-ignore]');
    if (ignored) {
      coverage.ignored++;
      exclusions.push({ selector: name(ignored), reason: ignored.getAttribute('data-film-review-ignore').trim() });
      continue;
    }
    if (!reliable(el)) { coverage.uncertain++; continue; }
    coverage.checked++;
    const level = el.closest('pre,code,[data-film-review-overflow]') ? 'error' : 'warning';
    if (rects.some(r => r.left < -1 || r.top < -1 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1)) {
      findings.push({ level, code: 'text_outside_viewport', selector: name(el), text: node.textContent.trim().slice(0, 100) });
    }
    for (let p = el; p; p = p.parentElement) {
      // Root overflow clips at the viewport, already checked above. A positioned
      // scene with a viewport containing block also escapes the static body's box.
      if (p === document.documentElement) continue;
      if (p === document.body && staticBody) {
        let escapes = false;
        for (let child = el; child !== p; child = child.parentElement) {
          const position = getComputedStyle(child).position;
          if ((position === 'absolute' && child.offsetParent === p) || (position === 'fixed' && child.offsetParent === null)) { escapes = true; break; }
        }
        if (escapes) continue;
      }
      const s = getComputedStyle(p), r = p.getBoundingClientRect();
      const sx = p.offsetWidth ? r.width / p.offsetWidth : 1, sy = p.offsetHeight ? r.height / p.offsetHeight : 1;
      const left = r.left + p.clientLeft * sx, top = r.top + p.clientTop * sy;
      const clipsX = /^(hidden|clip|scroll|auto)$/.test(s.overflowX), clipsY = /^(hidden|clip|scroll|auto)$/.test(s.overflowY);
      if (rects.some(t => (clipsX && (t.left < left - 1 || t.right > left + p.clientWidth * sx + 1)) ||
        (clipsY && (t.top < top - 1 || t.bottom > top + p.clientHeight * sy + 1)))) {
        findings.push({ level, code: 'text_clipped_by_container', selector: name(el), container: name(p) });
        break;
      }
    }
  }
  for (const el of document.querySelectorAll('pre,code,[data-film-review-overflow]')) {
    if (!visible(el) || el.closest('[data-film-review-ignore]')) continue;
    coverage.marked++;
    if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
      findings.push({ level: 'error', code: 'element_overflow', selector: name(el) });
    }
  }
  // Apply prose rules only to authored copy, never to real UI, commands or URLs.
  for (const el of document.querySelectorAll('[data-film-copy]')) {
    if (!visible(el) || el.closest('pre,code')) continue;
    const text = el.innerText.trim();
    if (/\b(?:illustrated example|example session|\d+ of \d+ questions)\b/i.test(text)) {
      findings.push({level: 'error', code: 'production_label', selector: name(el), text: text.slice(0, 160)});
    }
    if (/\S\s+[·•.]\s+\S/.test(text)) {
      findings.push({level: 'warning', code: 'decorative_copy', selector: name(el), text: text.slice(0, 160)});
    }
  }
  return { findings, exclusions, coverage, unsupported: { canvas: document.querySelectorAll('canvas').length,
    svg_text: document.querySelectorAll('svg text').length, iframe: document.querySelectorAll('iframe').length } };
}

export async function reviewFrames({ page: source, outdir, w = 1920, h = 1080, fps = 60 }) {
  const report = { status: 'failed', width: w, height: h, fps, frames_checked: 0, events: [], snapshots: [], repeat_seeks: [],
    errors: [], warnings: [], exclusions: [], coverage: { visible: 0, offscreen_marked: 0, checked: 0, ignored: 0, uncertain: 0, marked: 0 },
    maximum_manual_surfaces: { canvas: 0, svg_text: 0, iframe: 0 },
    limits: ['DOM checks cannot prove occlusion, canvas, SVG, CSS-generated or iframe text.', 'Rotated and 3D text needs visual review.',
      'Code and explicitly marked containers get strict checks; other clipping is a warning.',
      'Event previews use source times plus/minus one frame, clamped to rendered frames.',
      'Repeated seeks allow the same small antialiasing noise as the existing determinism tool.'] };
  mkdirSync(outdir, { recursive: true });
  const issues = new Map(), excluded = new Map();
  const expires = Date.now() + 180000;
  const bounded = async (operation, label, milliseconds = Math.max(1, Math.min(10000, expires - Date.now()))) => {
    let timer;
    try { return await Promise.race([operation, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} timed out`)), milliseconds); })]); }
    finally { clearTimeout(timer); }
  };
  let browser, time = null;
  const issue = (level, item) => {
    const key = JSON.stringify([level, item]);
    if (!issues.has(key)) issues.set(key, { level, ...item, first_time: time, last_time: time, count: 0 });
    const row = issues.get(key); row.count++; row.last_time = time;
  };
  try {
    if (![w, h, fps].every(n => Number.isInteger(n) && n > 0) || w > 4096 || h > 4096 || fps > 120) {
      throw new Error('Use integer width/height 1–4096 and FPS 1–120');
    }
    const policy = createFilmPolicy(resolve(source)), pagePath = policy.page;
    const { chromium } = require('playwright'), { PNG } = require('playwright-core/lib/utilsBundle');
    browser = await launchFilmBrowser(chromium);
    const context = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, serviceWorkers: 'block', acceptDownloads: false });
    await installFilmRoutes(context, policy, item => issue('error', item));
    const page = await context.newPage();
    page.on('pageerror', error => issue('error', { code: 'browser_error', message: error.message.slice(0, 500) }));
    page.on('console', message => { if (message.type() === 'error') issue('error', { code: 'console_error', message: message.text().slice(0, 500) }); });
    page.on('requestfailed', request => issue('error', { code: 'request_failed', url: request.url().slice(0, 500), message: request.failure()?.errorText }));
    const url = policy.url({ render: '1', w: String(w), h: String(h), fps: String(fps), sub: '1', shutter: '270' });
    await page.goto(url.href, { timeout: 15000 });
    await page.waitForFunction(() => typeof window.seek === 'function' && window.FILM, undefined, { timeout: 10000 });
    await bounded(page.evaluate(() => document.fonts.ready), 'Fonts');
    const metadata = await bounded(page.evaluate(() => {
      const { duration, width, height, events, scenes = [] } = window.FILM;
      if (!Number.isFinite(duration) || duration <= 0) throw new Error('FILM.duration must be finite and positive');
      if (![width, height].every(n => Number.isFinite(n) && n > 0)) throw new Error('FILM.width/height must be finite and positive');
      if (!Array.isArray(events) || !events.length || events.length > 64) throw new Error('FILM.events needs 1–64 named source events');
      if (events.some(e => !e || typeof e.name !== 'string' || !e.name.trim() || e.name.length > 120 || !Number.isFinite(e.t) || e.t < 0 || e.t >= duration)) {
        throw new Error('FILM.events needs nonempty names and finite times inside the film');
      }
      if (new Set(events.map(e => e.name)).size !== events.length) throw new Error('FILM.events names must be unique');
      if (events.filter(e => e.role === 'result').length !== 1) throw new Error('One FILM.events entry must have role: result');
      if (events.some(e => e.grid !== undefined && !['beat', 'half', 'quarter', 'bar', 'transient'].includes(e.grid))) throw new Error('Unknown FILM.events grid');
      if (!Array.isArray(scenes) || scenes.length > 64) throw new Error('FILM.scenes must be a list of at most 64 scenes');
      for (let i = 0; i < scenes.length; i++) {
        const scene = scenes[i];
        if (!scene || typeof scene.name !== 'string' || !scene.name.trim() ||
            !Number.isFinite(scene.from) || !Number.isFinite(scene.to) || scene.from < 0 || scene.to <= scene.from || scene.to > duration ||
            (i === 0 && scene.from !== 0) || (i > 0 && Math.abs(scene.from - scenes[i-1].to) > 0.00001)) {
          throw new Error('Scenes must cover the film in order, without gaps or overlaps');
        }
        if (scene.kind === 'closing' && i !== scenes.length - 1) throw new Error('A closing scene cannot precede more story');
      }
      if (scenes.length && Math.abs(scenes.at(-1).to - duration) > 0.00001) throw new Error('Scenes must include the final frame');
      return { duration, width, height, scenes, events: events.map(({ name, t, role, grid }) => ({ name, t, ...(role ? { role } : {}), ...(grid ? { grid } : {}) })) };
    }), 'Film metadata');
    Object.assign(report, { duration: metadata.duration, source_width: metadata.width, source_height: metadata.height, events: metadata.events, scenes: metadata.scenes, visual_review: "pending", interface_review: "pending" });
    if (!metadata.scenes.length) issue('warning', {code: 'missing_scene_coverage'});
    for (const scene of metadata.scenes) {
      if (scene.kind === 'product' && !scene.reference) issue('warning', {code: 'missing_interface_reference', scene: scene.name});
      if (scene.reference) {
        if (!scene.reference.image || !/^https?:\/\//.test(scene.reference.source || '')) throw new Error('UI reference needs a local image and its source URL');
        const source = policy.asset(resolve(dirname(pagePath), scene.reference.image), {imageOnly: true});
        const file = `reference-${metadata.scenes.indexOf(scene)}${extname(source)}`;
        writeFileSync(join(outdir, file), readFileSync(source));
        scene.reference = {...scene.reference, file};
      }
    }
    const ignores = await bounded(page.evaluate(() => [...document.querySelectorAll('[data-film-review-ignore]')].map(el => {
      const parts = [];
      for (let p = el; p; p = p.parentElement) {
        if (p.id) { parts.unshift(`#${p.id}`); break; }
        parts.unshift(`${p.tagName.toLowerCase()}:nth-child(${p.parentElement ? [...p.parentElement.children].indexOf(p) + 1 : 1})`);
      }
      return { selector: parts.join(' > '), reason: el.getAttribute('data-film-review-ignore').trim() };
    })), 'Exclusion metadata');
    for (const item of ignores) excluded.set(JSON.stringify(item), { ...item, observations: 0, first_time: null, last_time: null });
    writeFileSync(join(outdir, 'events.json'), JSON.stringify(metadata.events, null, 2) + '\n');
    const total = Math.round(metadata.duration * fps);
    if (total < 1 || total > 10800) throw new Error('Film review needs 1–10800 frames');
    report.total_frames = total;
    const seek = async t => {
      time = t;
      await bounded(page.evaluate(async ({ t, duration, events }) => {
        let timer;
        try {
          await Promise.race([Promise.resolve().then(() => window.seek(t)), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('seek(t) timed out')), 5000); })]);
        } finally { clearTimeout(timer); }
        if (window.FILM.duration !== duration || JSON.stringify(window.FILM.events.map(({ name, t, role, grid }) => ({ name, t, ...(role ? { role } : {}), ...(grid ? { grid } : {}) }))) !== events) {
          throw new Error('FILM duration/events changed during seek');
        }
        const seen = new Set();
        const finite = (value, path) => {
          if (typeof value === 'number' && !Number.isFinite(value)) throw new Error(`Nonfinite FRAME value: ${path}`);
          if (value && typeof value === 'object' && !seen.has(value)) {
            seen.add(value); for (const [key, child] of Object.entries(value)) finite(child, `${path}.${key}`);
          }
        };
        finite(window.FRAME, 'FRAME');
      }, { t, duration: metadata.duration, events: JSON.stringify(metadata.events) }), 'seek/browser');
    };
    for (let frame = 0; frame < total; frame++) {
      await seek(frame / fps);
      const found = await bounded(page.evaluate(inspectText), 'Frame text inspection');
      for (const item of found.findings) { const { level, ...detail } = item; issue(level, detail); }
      for (const [key, count] of Object.entries(found.coverage)) report.coverage[key] += count;
      for (const [key, count] of Object.entries(found.unsupported)) report.maximum_manual_surfaces[key] = Math.max(report.maximum_manual_surfaces[key], count);
      for (const item of found.exclusions) {
        const key = JSON.stringify(item);
        if (!excluded.has(key)) excluded.set(key, { ...item, observations: 0, first_time: time, last_time: time });
        const row = excluded.get(key); row.observations++; row.first_time ??= time; row.last_time = time;
      }
      report.frames_checked++;
    }
    if (report.coverage.uncertain) issue('warning', { code: 'unverified_text_geometry', observations: report.coverage.uncertain });
    if (!report.coverage.checked) issue('warning', { code: 'no_verified_text', observations: report.coverage.visible });
    if (report.coverage.ignored && report.coverage.ignored === report.coverage.visible + report.coverage.offscreen_marked) issue('error', { code: 'all_text_excluded' });
    if (Object.values(report.maximum_manual_surfaces).some(Boolean)) issue('warning', { code: 'manual_visual_surfaces', ...report.maximum_manual_surfaces });
    const end = (total - 1) / fps;
    const samples = [{ name: 'opening', t: 0 }, { name: 'result', t: Math.min(end, metadata.events.find(e => e.role === 'result').t) }, { name: 'end', t: end }];
    metadata.events.forEach((event, i) => {
      if (event.t > end) issue('warning', { code: 'event_after_last_frame', event: event.name, source_time: event.t, last_frame: end });
      for (const [offset, label] of [[-1, 'before'], [0, 'at'], [1, 'after']]) {
        samples.push({ name: `event-${String(i + 1).padStart(2, '0')}-${label}`, event: event.name, source_time: event.t, t: Math.max(0, Math.min(end, event.t + offset / fps)) });
      }
    });
    metadata.scenes.forEach((scene, i) => {
      for (const [label, t] of [['start', scene.from], ['middle', (scene.from + scene.to) / 2], ['end', scene.to - 1 / fps]]) {
        samples.push({name: `scene-${i + 1}-${label}`, scene: scene.name, reference: scene.reference, t: Math.max(scene.from, Math.min(end, t))});
      }
    });
    for (const back of [2, 1, 0]) samples.push({name: `ending-${back}`, t: Math.max(0, end - back)});
    const baseline = new Map();
    for (const sample of samples.sort((a, b) => a.t - b.t)) {
      await seek(sample.t);
      const png = await bounded(page.screenshot({ type: 'png', timeout: 10000 }), 'Frame capture'), file = `${sample.name}.png`;
      writeFileSync(join(outdir, file), png);
      baseline.set(sample.t, file);
      report.snapshots.push({ ...sample, file });
    }
    const times = [...baseline.keys()];
    for (const [order, values] of [['backward', [...times].reverse()], ['shuffled', [...times].sort((a, b) => ((a * 7919) % 13) - ((b * 7919) % 13))]]) {
      for (const t of values) {
        await seek(t);
        const a = PNG.sync.read(readFileSync(join(outdir, baseline.get(t)))).data;
        const b = PNG.sync.read(await bounded(page.screenshot({ type: 'png', timeout: 10000 }), 'Repeat capture')).data;
        let pixels = 0, maximum = 0;
        for (let i = 0; i < a.length; i += 4) {
          const d = Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i + 1] - b[i + 1]), Math.abs(a[i + 2] - b[i + 2]), Math.abs(a[i + 3] - b[i + 3]));
          if (d) { pixels++; maximum = Math.max(maximum, d); }
        }
        const noise = pixels > 0 && (maximum <= 6 || (maximum <= 12 && pixels < w * h * .001));
        report.repeat_seeks.push({ t, order, pixels, maximum, raster_noise: noise });
        if (pixels && !noise) issue('error', { code: 'history_dependent_pixels', t, order, pixels, maximum });
      }
    }
  } catch (error) {
    issue('error', { code: 'review_failed', message: error.message.slice(0, 1000) });
  } finally {
    if (browser) {
      try { await bounded(browser.close(), 'Browser cleanup', 5000); }
      catch (error) { issue('error', { code: 'cleanup_failed', message: error.message }); }
    }
    report.errors = [...issues.values()].filter(row => row.level === 'error');
    report.warnings = [...issues.values()].filter(row => row.level === 'warning');
    report.exclusions = [...excluded.values()];
    const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    writeFileSync(join(outdir, 'index.html'), `<!doctype html><meta charset="utf-8"><title>Film review</title><style>body{font:16px system-ui;background:#eee;padding:24px}section{margin:24px 0}img{max-width:360px;vertical-align:top;margin:8px}h2{font-size:18px}</style><h1>Scene review</h1><p>Geometry and timing checks do not approve design or product accuracy. Compare every scene and read report.json.</p>${report.snapshots.map(s => `<section><h2>${escape(s.scene || s.name)} · ${s.t.toFixed(3)} s</h2><img src="${escape(s.file)}" alt="Rendered frame">${s.reference ? `<img src="${escape(s.reference.file)}" alt="Real interface reference"><p>Source: ${escape(s.reference.source)}</p>` : ''}</section>`).join('')}`);
    report.status = report.errors.length ? 'failed' : report.warnings.length || report.exclusions.length ? 'needs_visual_review' : 'checked';
    writeFileSync(join(outdir, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  }
  return report;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const arg = (name, fallback) => { const i = process.argv.indexOf(`--${name}`); return i >= 0 ? process.argv[i + 1] : fallback; };
  const report = await reviewFrames({ page: arg('page', 'src/index.html'), outdir: resolve(arg('outdir', 'out/frame-review')),
    w: Number(arg('w', 1920)), h: Number(arg('h', 1080)), fps: Number(arg('fps', 60)) });
  console.log(JSON.stringify({ status: report.status, frames_checked: report.frames_checked, errors: report.errors.length, warnings: report.warnings.length }));
  process.exitCode = report.errors.length ? 1 : 0;
}
