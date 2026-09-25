// Real packaged renderer, CSS, fonts, and catalog data; no authenticated services.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";
import { diagramFixtures } from "./diagram-fixtures.js";
import { routingFixtures } from "./diagram-routing-fixtures.js";
import { auditDiagrams } from "./diagram-audit.js";

const assets = path.resolve("src/tin_lite/static");
const catalog = JSON.parse(execFileSync("uv", ["run", "--frozen", "python", "-c", [
  "import json",
  "from tin_lite.catalog import BUILTIN_WORKFLOWS",
  "print(json.dumps([{'title': w.title, 'flow': w.definition['presentation']['flow']} for w in BUILTIN_WORKFLOWS if w.presentation]))",
].join("\n")], { encoding: "utf8" }));

const brandFixtures = [
  {light: {ink: "#18242C", paper: "#FFFFFF", accent: "#2265BD"}},
  {light: {ink: "#492136", paper: "#FFF9ED", accent: "#CE9645"}},
  {light: {ink: "#10241C", paper: "#EDF5EF", accent: "#58CC99"}, dark: {ink: "#EDF5EF", paper: "#10241C", accent: "#58CC99"}},
].map((palette, index) => ({
  id: `brand-${index}`, title: `Synthetic brand ${index}`,
  source: `graph LR\n  %% tin:brand ${JSON.stringify({revision: "a".repeat(40), sha256: String(index + 1).repeat(64), ...palette})}\n  ask["Propose a change"]:::step\n  review["Human review"]:::gate\n  done["Verified evidence"]:::receipt\n  ask --> review\n  review --> done\n`,
}));

const studies = await Promise.all(JSON.parse(await fs.readFile("docs/diagram-studies/index.json", "utf8")).map(async (item) => ({ ...item, source: await fs.readFile(`docs/diagram-studies/${item.file}`, "utf8") })));

test("catalog and varied diagrams keep balanced typography and clear routes across product surfaces", async () => {
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/") {
      response.setHeader("Content-Type", "text/html");
      response.end(`<!doctype html><html><head><link rel="stylesheet" href="/assets/app.css">
        <style>body{padding:24px;background:var(--paper-bg)}main{max-width:1360px;margin:auto}
        section{margin-bottom:32px;min-width:0}h2{font:700 18px var(--diagram-sans);margin-bottom:16px}
        #untouched text{font-family:serif}#untouched{width:20px;height:20px}</style></head>
        <body><main><svg id="untouched"><text>Other SVG</text></svg><div id="diagrams"></div></main>
        <script src="/assets/diagram-renderer.js"></script><script src="/assets/diagram-loader.js"></script>
        <script src="/assets/markdown-viewer.js"></script></body></html>`);
      return;
    }
    const file = path.resolve(assets, url.pathname.replace(/^\/assets\//, ""));
    if (!file.startsWith(`${assets}/`)) return response.writeHead(404).end();
    try {
      const type = file.endsWith(".wasm") ? "application/wasm" : file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : "font/woff2";
      response.setHeader("Content-Type", type);
      response.end(await fs.readFile(file));
    } catch { response.writeHead(404).end(); }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(process.env.TIN_TEST_BROWSER_CHANNEL ? { channel: process.env.TIN_TEST_BROWSER_CHANNEL } : {}) });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await page.goto(base);
    for (const theme of ["light", "dark"]) {
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: 960 });
        for (const surface of ["workflow-diagram", "project-diagram-canvas", "md-diagram-stage"]) {
          await page.evaluate(async ({ catalog, theme, surface }) => {
            document.documentElement.dataset.theme = theme;
            const root = document.getElementById("diagrams");
            root.replaceChildren();
            for (const [index, item] of catalog.entries()) {
              const section = document.createElement("section");
              section.id = item.id || `catalog-${index}`;
              const heading = document.createElement("h2");
              heading.textContent = item.title;
              const stage = document.createElement("div");
              stage.className = `tin-diagram ${surface}`;
              stage.innerHTML = item.source ? await window.TinDiagramRenderer.renderSource(item.source) : (await window.TinDiagramRenderer.renderFlow(item.flow)).svg;
              section.append(heading, stage); root.append(section);
            }
          }, { catalog: [...catalog, ...diagramFixtures, ...routingFixtures, ...studies, ...brandFixtures], theme, surface });
          await page.evaluate(async () => {
            await Promise.all([400, 700].flatMap((weight) => ["Tin Diagram Sans", "Tin Diagram Mono"].map((family) => document.fonts.load(`${weight} 12px "${family}"`))));
            await document.fonts.ready;
          });
          const audit = await page.evaluate(auditDiagrams);
          assert.deepEqual(audit.issues, [], `${theme}/${width}/${surface}: visual geometry`);
          const measured = await page.evaluate(() => {
            const issues = [];
            const stages = [...document.querySelectorAll(".tin-diagram")];
            for (const stage of stages) {
              const svg = stage.querySelector("svg");
              if (Math.abs(svg.getScreenCTM().a - 1) > 0.02) issues.push("diagram text was scaled");
              if (svg.getBoundingClientRect().left < stage.getBoundingClientRect().left) issues.push("start of diagram is clipped");
              for (const group of svg.querySelectorAll("g.node")) {
                const rect = group.querySelector("rect").getBBox();
                for (const line of group.querySelectorAll("tspan")) {
                  const box = line.getBBox();
                  if (box.x < rect.x || box.x + box.width > rect.x + rect.width) issues.push(`${group.dataset.id} label overflow: ${line.textContent}`);
                }
              }
              // Every part of a wide diagram remains reachable by scrolling its surface.
              stage.scrollLeft = stage.scrollWidth;
              if (svg.getBoundingClientRect().right > stage.getBoundingClientRect().right + 1) issues.push("end of diagram is unreachable");
              stage.scrollLeft = 0;
            }
            return {
              issues,
              overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
              fonts: [...document.fonts].filter((font) => font.family.startsWith("Tin Diagram")).map((font) => ({ family: font.family, status: font.status })),
              unrelatedFont: getComputedStyle(document.querySelector("#untouched text")).fontFamily,
              arrow: getComputedStyle(document.querySelector(".tin-diagram-arrow")).stroke,
              shaft: getComputedStyle(document.querySelector(".edge")).stroke,
              arrowFill: getComputedStyle(document.querySelector(".tin-diagram-arrow")).fill,
            };
          });
          assert.deepEqual(measured.issues, [], `${theme}/${width}/${surface}`);
          assert.equal(measured.overflow, false, `${theme}/${width}/${surface}: page overflow`);
          assert.equal(measured.fonts.length, 4);
          assert.ok(measured.fonts.every((font) => font.status === "loaded"));
          assert.equal(measured.unrelatedFont, "serif");
          assert.equal(measured.arrow, measured.shaft);
          assert.equal(measured.arrow, theme === "light" ? "rgb(146, 144, 135)" : "rgb(125, 120, 111)");
          assert.equal(measured.arrowFill, "none");
          if (process.env.TIN_DIAGRAM_SCREENSHOTS && surface === "workflow-diagram") {
            await fs.mkdir(process.env.TIN_DIAGRAM_SCREENSHOTS, { recursive: true });
            await page.screenshot({ path: path.join(process.env.TIN_DIAGRAM_SCREENSHOTS, `${theme}-${width}.png`), fullPage: true });
          }
        }
      }
    }
    // Switch themes without rerendering: each SVG owns its colors, the host does not.
    for (const theme of ["light", "dark"]) {
      const colors = await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme;
        return [0,1,2].map(i => {
          const svg = document.querySelector(`#brand-${i} svg`);
          return {paper: getComputedStyle(svg).backgroundColor,
            ink: getComputedStyle(svg.querySelector(".node tspan")).fill,
            accent: getComputedStyle(svg.querySelector(".tin-diagram-receipt circle")).fill};
        });
      }, theme);
      assert.equal(colors[0].paper, "rgb(255, 255, 255)");
      assert.equal(colors[1].paper, "rgb(255, 249, 237)");
      assert.equal(colors[1].ink, "rgb(73, 33, 54)");
      assert.equal(colors[1].accent, "rgb(206, 150, 69)");
      assert.equal(colors[2].paper, theme === "dark" ? "rgb(16, 36, 28)" : "rgb(237, 245, 239)");
      assert.equal(colors[2].ink, theme === "dark" ? "rgb(237, 245, 239)" : "rgb(16, 36, 28)");
    }
    // Exercise the actual async Markdown consumer, including its source fallback.
    const source = await page.evaluate((source) => {
      const article = document.createElement("article");
      for (const value of [source, 'graph LR\n click bad javascript:alert(1)']) {
        const pre = document.createElement("pre");
        pre.className = "md-code-block"; pre.dataset.language = "mermaid";
        const code = document.createElement("code"); code.textContent = value;
        pre.append(code); article.append(pre);
      }
      window.TinMarkdownViewer.mount(document.getElementById("diagrams"), {
        filename: "workflow.md", html: article.innerHTML, word_count: 20, reading_minutes: 1,
      }, { mode: "in-app" });
      return source;
    }, brandFixtures[2].source);
    await page.locator(".md-diagram-stage svg").waitFor();
    assert.equal(await page.locator(".md-diagram-source code").textContent(), source);
    assert.equal(await page.locator(".md-diagram-stage .node").count(), (brandFixtures[2].source.match(/:::/g) || []).length);
    await page.locator(".md-code-block.is-diagram-invalid").waitFor();
    assert.doesNotMatch(await page.locator("#diagrams").textContent(), /\[object Promise\]/);
    assert.deepEqual(errors, []);
    // A failed lazy WASM fetch must not poison later renders or trigger separate
    // initializations when two surfaces retry together. Flat flows need no WASM.
    const retryPage=await browser.newPage();
    let rejectWasm=true,wasmRequests=0;
    await retryPage.route("**/*", route=>{
      if(!route.request().url().startsWith(base))return route.abort();
      if(route.request().url().includes("diagram-routing.wasm")) {
        wasmRequests++;
        if(rejectWasm)return route.fulfill({status:503,body:"temporarily unavailable"});
      }
      return route.continue();
    });
    await retryPage.goto(base);
    await retryPage.evaluate(flow=>window.TinDiagramRenderer.renderFlow(flow),catalog[0].flow);
    assert.equal(wasmRequests,0,"flat flows do not fetch the composition kernel");
    const failed=await retryPage.evaluate(async source=>{
      try {await window.TinDiagramRenderer.renderSource(source);return false;}
      catch {return true;}
    },studies[0].source);
    assert.equal(failed,true);
    rejectWasm=false;
    const beforeRetry=wasmRequests;
    const retries=await retryPage.evaluate(source=>Promise.all([1,2].map(()=>window.TinDiagramRenderer.renderSource(source))),studies[0].source);
    assert.equal(retries[0],retries[1]);
    assert.equal(wasmRequests-beforeRetry,1,"concurrent surfaces share one kernel initialization");
    await retryPage.close();
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
