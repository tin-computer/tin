#!/usr/bin/env node
// No web access, model call, project writes, or user-supplied HTML/JavaScript.
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { parseSource } from "../web/diagram-contract.js";
import { auditDiagrams } from "../web/diagram-audit.js";
import { diagramQuality, readDiagramGeometry } from "../web/diagram-quality.js";
import { embeddedFonts, loadFonts, exportDiagrams } from "./diagram-review.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const assets = path.join(root, "src/tin_lite/static");
export const CHECKER_VERSION = "tin-diagram-check.v1";
const digest = (raw) => createHash("sha256").update(raw).digest("hex");

export async function checkDiagram(candidate, output, { previews = true } = {}) {
  const stat = await fs.lstat(candidate).catch(() => null);
  if (!stat?.isFile() || stat.size > 64000 || stat.size === 0) return { checker: CHECKER_VERSION, source_sha256: null, source_bytes: stat?.size || 0, passed: false, issues: ["Candidate must be a regular UTF-8 file of 1–64000 bytes"], themes: [], previews: [] };
  const raw = await fs.readFile(candidate);
  const source = new TextDecoder("utf-8", { fatal: true }).decode(raw);
  const report = { checker: CHECKER_VERSION, source_sha256: digest(raw), source_bytes: raw.length, passed: false, issues: [], themes: [], previews: [] };
  try { parseSource(source); } catch (error) {
    report.issues = [String(error.message).slice(0, 500)];
    return report; // Fail grammar before launching a browser.
  }
  await fs.mkdir(output, { recursive: true });
  const resources = ["app.css", "diagram-renderer.js", "diagram-routing.wasm", "fonts/geist-sans-regular.woff2", "fonts/geist-sans-bold.woff2", "fonts/geist-mono-regular.woff2", "fonts/geist-mono-bold.woff2"];
  const files = new Map(await Promise.all(resources.map(async (name) => [name, await fs.readFile(path.join(assets, name))])));
  report.renderer_sha256 = digest(Buffer.concat([...files.values()]));
  const fontRules = await embeddedFonts(assets);
  if (await fs.stat(path.join(root, "browsers")).catch(() => null)) process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(root, "browsers");
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true, env: { PATH: process.env.PATH, HOME: process.env.HOME }, args: ["--disable-background-networking"] });
  try {
    report.browser_version = browser.version();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1, serviceWorkers: "block" });
    page.setDefaultTimeout(30000);
    // Fulfill *every* request from a fixed in-memory allowlist; no localhost server,
    // external requests, proxies, or code from the project checkout.
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== "http://tin-diagram.invalid") return route.abort();
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body: '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css"><style>body{margin:0;padding:24px;background:var(--paper-bg)}section{width:max-content}.tin-diagram{overflow:visible}svg{margin:0!important}</style><section id="candidate"><div class="tin-diagram"></div></section><script src="/diagram-renderer.js"></script>' });
      const name = url.pathname.replace(/^\/(?:assets\/)?/, ""), body = files.get(name);
      if (!body) return route.abort();
      return route.fulfill({ body, contentType: name.endsWith(".wasm") ? "application/wasm" : name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "font/woff2" });
    });
    await page.goto("http://tin-diagram.invalid/");
    await page.evaluate(loadFonts);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(async ({ source, theme }) => {
        document.documentElement.dataset.theme = theme;
        document.querySelector(".tin-diagram").innerHTML = await window.TinDiagramRenderer.renderSource(source);
      }, { source, theme });
      const audit = await page.evaluate(auditDiagrams);
      const [geometry] = await page.evaluate(readDiagramGeometry);
      report.themes.push({ theme, issues: audit.issues.slice(0, 50), quality: diagramQuality(geometry) });
      report.issues.push(...audit.issues.slice(0, 50).map((issue) => `${theme}: ${issue.slice(0, 500)}`));
      if (!previews) continue;
      const [exported] = await page.evaluate(exportDiagrams, { fontRules });
      await fs.writeFile(path.join(output, `${theme}.svg`), exported.svg);
      // Overview is bounded for the API request budget. Natural-scale detail tiles
      // remain available to the agent's image viewer; no tiny unreadable mega-canvas.
      const box = await page.locator("svg").boundingBox();
      if (!box || box.width > 16000 || box.height > 16000) throw new Error("Diagram exceeds preview dimensions");
      const scale = Math.min(1, 1320 / box.width, 900 / box.height);
      await page.locator("svg").evaluate((svg, scale) => { svg.style.width = `${svg.viewBox.baseVal.width * scale}px`; svg.style.height = `${svg.viewBox.baseVal.height * scale}px`; }, scale);
      const overview = path.join(output, `${theme}.png`);
      await page.locator("svg").screenshot({ path: overview });
      report.previews.push(overview);
      await page.locator("svg").evaluate((svg) => { svg.style.removeProperty("width"); svg.style.removeProperty("height"); });
      if (theme === "light" && scale < 0.85) {
        const columns = Math.ceil(box.width / 1100), rows = Math.ceil(box.height / 800);
        if (columns * rows > 24) throw new Error("Composition is too spread out to inspect: consolidate groups without removing facts");
        await page.setViewportSize({ width: Math.ceil(box.width + 48), height: Math.ceil(box.height + 48) });
        for (let row = 0; row < rows; row++) for (let column = 0; column < columns; column++) {
          await page.screenshot({ path: path.join(output, `detail-${row}-${column}.png`), clip: { x: box.x + column * 1100, y: box.y + row * 800, width: Math.min(1100, box.width - column * 1100), height: Math.min(800, box.height - row * 800) } });
        }
      }
    }
    report.passed = report.issues.length === 0;
  } catch (error) {
    report.issues.push(`Render check failed: ${String(error.message).slice(0, 700)}`);
  } finally { await browser.close(); }
  if (digest(await fs.readFile(candidate)) !== report.source_sha256) {
    report.issues.push("Candidate changed during inspection"); report.passed = false;
  }
  return report;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv[2] === "--version") console.log(CHECKER_VERSION);
  else if (process.argv[2] === "--brand-version") console.log("tin-diagram.branded.v1");
  else {
    const [, , verb, candidate, flag, output, mode] = process.argv;
    if (verb !== "check" || !candidate || flag !== "--out" || !path.isAbsolute(output || "")) throw new Error("Usage: tin-diagram check candidate.mmd --out /absolute/temporary-directory [--no-previews]");
    const report = await checkDiagram(candidate, output, { previews: mode !== "--no-previews" });
    await fs.mkdir(output, { recursive: true });
    await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
    // The unprivileged author may create previews under umask 077. Permit the
    // controller's localImage reader to inspect these derived, credential-free files.
    await fs.chmod(output, 0o755);
    for (const name of await fs.readdir(output)) await fs.chmod(path.join(output, name), 0o644);
    console.log(JSON.stringify(report));
    process.exitCode = report.passed ? 0 : 1;
  }
}
