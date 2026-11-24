// Exercise the real Files route, packaged renderer, and navigation with isolated
// HTTP fixtures. No live credentials, workflow starts, or approval effects.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";
import { sourceForFlow } from "./diagram-contract.js";
import { routingFixtures } from "./diagram-routing-fixtures.js";

const assets = path.resolve("src/tin_lite/static");
const revision = "a".repeat(40);
const chain = (direction) => `graph ${direction}\n  %% tin:composition\n  subgraph sequence\n    direction ${direction}\n    %% tin:group layout\n${Array.from({ length: 32 }, (_, i) => `    node_${i}["Step ${i + 1}<br/>A deliberately long sequence"]:::step`).join("\n")}\n  end\n${Array.from({ length: 31 }, (_, i) => `  node_${i} --> node_${i + 1}`).join("\n")}\n`;
const fixtures = new Map([
  ["small.mmd", 'graph LR\n  a["Prepare"]:::step\n  b["Ready"]:::receipt\n  a --> b\n'],
  ["wide.mmd", chain("LR")], ["tall.mmd", chain("TD")],
  ["review.mmd", sourceForFlow(routingFixtures.find((f) => f.id === "composed-review-chain").flow)],
  ["invalid.mmd", 'graph TD\n  click a "javascript:alert(1)"'],
]);
for (const name of ["whole-system", "session-lifecycle", "stranger-walk"]) {
  fixtures.set(`${name}.mmd`, await fs.readFile(`docs/diagram-studies/${name}.mmd`, "utf8"));
}

async function startServer() {
  const project = { id: "project", name: "Diagram review", workspace_id: "workspace", workspace_name: "QA", member_count: 1, timezone: "UTC" };
  const writes = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const send = (value, type = "application/json") => {
      response.setHeader("Content-Type", type);
      const body = type === "application/json" ? JSON.stringify(value) : value;
      response.setHeader("Content-Length", Buffer.byteLength(body));
      response.end(body);
    };
    if (url.pathname.startsWith("/assets/")) {
      const file = path.resolve(assets, url.pathname.slice(8));
      if (!file.startsWith(`${assets}/`)) return response.writeHead(404).end();
      try { send(await fs.readFile(file), file.endsWith(".css") ? "text/css" : file.endsWith(".js") ? "text/javascript" : file.endsWith(".wasm") ? "application/wasm" : "application/octet-stream"); }
      catch { response.writeHead(404).end(); }
      return;
    }
    if (/^\/(?:system|chat|activity|decisions|files|file|document\/[^/]+|task\/[^/]+|compare\/[^/]+)?$/.test(url.pathname)) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8"))
        .replace(/<script\b[^>]*src="\{\{CLERK[^>]+>[\s\S]*?<\/script>/g, "").replaceAll("{{ASSET_VERSION}}", "test");
      return send(html, "text/html");
    }
    if (request.method !== "GET") writes.push({ url: request.url, method: request.method });
    if (url.pathname === "/api/projects") return send([project]);
    if (url.pathname.endsWith("/system")) return send({ waiting_count: 0, running_count: 0, workflow_count: 0 });
    if (url.pathname.endsWith("/files/raw")) {
      assert.equal(url.searchParams.get("revision"), revision);
      const source = fixtures.get(url.searchParams.get("path"));
      if (!source) return response.writeHead(404).end();
      return send(source, "text/vnd.mermaid");
    }
    if (url.pathname.endsWith("/files")) return send({ revision, files: [...fixtures.keys()].map((name) => ({ path: name })) });
    if (url.pathname.startsWith("/api/")) return send([]);
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { server, base: `http://127.0.0.1:${server.address().port}`, writes };
}

const measure = () => {
  const canvas = document.querySelector("[data-project-diagram]");
  const svg = canvas.querySelector("svg");
  const bounds = (el) => {
    const { x, y, width, height, right, bottom } = el.getBoundingClientRect();
    return { x, y, width, height, right, bottom };
  };
  const transform = new DOMMatrixReadOnly(canvas.querySelector(".diagram-viewport-plane").style.transform);
  const obstructedControls = [...document.querySelectorAll(".project-diagram-controls button")].filter((button) => {
    const box = button.getBoundingClientRect();
    return !button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
  }).map((button) => button.textContent);
  return { canvas: bounds(canvas), svg: bounds(svg), controls: bounds(document.querySelector(".project-diagram-controls")),
    obstructedControls,
    scale: transform.a, x: transform.e, y: transform.f, intrinsic: { width: svg.viewBox.baseVal.width, height: svg.viewBox.baseVal.height },
    pageWidth: document.documentElement.scrollWidth, pageHeight: document.documentElement.scrollHeight,
    width: innerWidth, height: innerHeight, scrollY, text: [...svg.querySelectorAll("tspan")].map((node) => node.textContent).join("\n") };
};
function assertFits(value, label) {
  assert.ok(value.svg.x >= value.canvas.x + 20 && value.svg.y >= value.canvas.y + 20, `${label}: top/left padding`);
  assert.ok(value.svg.right <= value.canvas.right - 20 && value.svg.bottom <= value.canvas.bottom - 20, `${label}: whole diagram visible`);
  assert.ok(value.canvas.bottom <= value.height + 1, `${label}: canvas fits screen`);
  assert.ok(value.controls.bottom <= value.height && value.controls.y >= 0, `${label}: controls always reachable`);
  assert.ok(value.pageWidth <= value.width + 1 && value.pageHeight <= value.height + 1, `${label}: no page scrollbars (${value.pageWidth}×${value.pageHeight} vs ${value.width}×${value.height})`);
  assert.ok(value.scale <= 1, `${label}: small diagrams not enlarged on open`);
  assert.deepEqual(value.obstructedControls, [], `${label}: controls unobstructed`);
}

// Renders the whole catalog many times over: minutes on a loaded machine, so it gets more than
// the suite's default per-test timeout.
test("Files diagrams fit the available screen in both themes across aspect ratios", {timeout: 600000}, async () => {
  const { server, base, writes } = await startServer();
  const browser = await chromium.launch({ headless: true });
  const screenshots = process.env.TIN_VIEWPORT_SCREENSHOTS;
  if (screenshots) await fs.mkdir(screenshots, { recursive: true });
  try {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
      await context.addInitScript((value) => {
        window.Clerk = { load: async () => {}, isSignedIn: true, user: { id: "member", firstName: "QA" }, session: { getToken: async () => "synthetic-test-only" } };
        localStorage.setItem("tin-lite:theme", value);
      }, theme);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const open = async (name) => {
        await page.goto(`${base}/#file?${new URLSearchParams({ path: name, revision })}`);
        await page.locator(".project-diagram-canvas.is-interactive svg").waitFor();
        await page.waitForFunction(() => document.querySelector(".project-diagram-zoom output")?.value.includes("%"));
      };
      for (const size of [{ width: 1440, height: 900 }, { width: 900, height: 620 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
        await page.setViewportSize(size);
        for (const name of [...fixtures.keys()].filter((name) => name !== "invalid.mmd")) {
          await open(name);
          assertFits(await page.evaluate(measure), `${theme}/${size.width}/${name}`);
          if (screenshots && ["whole-system.mmd", "review.mmd", "tall.mmd"].includes(name)) {
            await page.screenshot({ path: path.join(screenshots, `${theme}-${size.width}-${name}.png`) });
          }
        }
      }
      await page.setViewportSize({ width: 1440, height: 900 });
      await open("review.mmd");
      const fit = await page.evaluate(measure);
      await page.getByRole("button", { name: "Zoom in", exact: true }).click();
      const enlarged = await page.evaluate(measure);
      assert.ok(enlarged.scale > fit.scale);
      assert.equal(enlarged.text, fit.text);
      await page.getByRole("button", { name: "Actual size", exact: true }).click();
      assert.equal((await page.evaluate(measure)).scale, 1);
      const viewport = page.getByRole("region", { name: "Interactive diagram" });
      await viewport.focus();
      const beforeKey = await page.evaluate(measure);
      await page.keyboard.press("ArrowDown");
      assert.ok((await page.evaluate(measure)).y < beforeKey.y);
      await page.keyboard.press("+");
      assert.equal((await page.evaluate(measure)).scale, 1.25);
      await page.keyboard.press("-");
      assert.equal((await page.evaluate(measure)).scale, 1);
      // Drag and native two-axis wheel pan without moving the document.
      const bounds = await viewport.boundingBox();
      const cx = bounds.x + bounds.width / 2, cy = bounds.y + bounds.height / 2;
      const beforeDrag = await page.evaluate(measure);
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      await page.mouse.move(cx - 30, cy - 70, { steps: 5 });
      await page.mouse.up();
      const dragged = await page.evaluate(measure);
      assert.ok(dragged.y < beforeDrag.y);
      await page.mouse.wheel(0, 80);
      await page.waitForFunction((y) => Number(new DOMMatrixReadOnly(document.querySelector(".diagram-viewport-plane").style.transform).f) < y, dragged.y);
      assert.equal((await page.evaluate(measure)).scrollY, 0);
      // Zoom anchors the point under the pointer while the diagram can move on both axes.
      await viewport.focus();
      await page.keyboard.press("+");
      await page.keyboard.press("+");
      const beforeZoom = await page.evaluate(measure);
      const ax = beforeZoom.canvas.width / 2 - 40, ay = beforeZoom.canvas.height / 2 - 40;
      await viewport.dispatchEvent("wheel", { deltaY: -20, ctrlKey: true,
        clientX: beforeZoom.canvas.x + 1 + ax, clientY: beforeZoom.canvas.y + 1 + ay });
      const afterZoom = await page.evaluate(measure);
      assert.ok(afterZoom.scale > beforeZoom.scale);
      assert.ok(Math.abs((ay - afterZoom.y) / afterZoom.scale - (ay - beforeZoom.y) / beforeZoom.scale) < 0.01, "pointer anchor is stable");
      // Mode switches preserve this opening's camera; source bytes are unchanged.
      const saved = await page.evaluate(measure);
      await page.getByRole("button", { name: "Source", exact: true }).click();
      assert.equal(await page.locator("[data-project-diagram] pre").textContent(), fixtures.get("review.mmd"));
      assert.equal(await page.getByRole("button", { name: "Zoom in", exact: true }).isVisible(), false);
      await page.getByRole("button", { name: "ASCII", exact: true }).click();
      await page.locator(".is-ascii pre").waitFor();
      assert.match(await page.locator(".is-ascii pre").textContent(), /Trigger/);
      await page.getByRole("button", { name: "Diagram", exact: true }).click();
      await page.locator(".is-interactive svg").waitFor();
      assert.equal((await page.evaluate(measure)).scale, saved.scale);
      // Theme changes retain the camera. Fit tracks resize; manual zoom does not reset.
      await page.evaluate((value) => window.TinTheme.apply(value), theme === "light" ? "dark" : "light");
      assert.equal((await page.evaluate(measure)).scale, saved.scale);
      await page.setViewportSize({ width: 1280, height: 780 });
      assert.equal((await page.evaluate(measure)).scale, saved.scale);
      await page.getByRole("button", { name: "Fit diagram", exact: true }).click();
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForFunction(() => {
        const canvas = document.querySelector("[data-project-diagram]").getBoundingClientRect();
        const svg = document.querySelector("[data-project-diagram] svg").getBoundingClientRect();
        return svg.x >= canvas.x + 20 && svg.y >= canvas.y + 20
          && svg.right <= canvas.right - 20 && svg.bottom <= canvas.bottom - 20;
      });
      assertFits(await page.evaluate(measure), `${theme}: responsive fit`);
      await viewport.focus();
      await page.keyboard.press("1");
      await page.keyboard.press("0");
      assertFits(await page.evaluate(measure), `${theme}: keyboard fit`);
      // Unsupported source stays inspectable and leaves zoom unavailable.
      await page.goto(`${base}/#file?${new URLSearchParams({ path: "invalid.mmd", revision })}`);
      await page.locator(".project-diagram-error").waitFor();
      assert.equal(await page.getByRole("button", { name: "Zoom in", exact: true }).isEnabled(), false);
      await page.getByRole("button", { name: "Source", exact: true }).click();
      assert.equal(await page.locator("[data-project-diagram] pre").textContent(), fixtures.get("invalid.mmd"));
      await open("review.mmd");
      assertFits(await page.evaluate(measure), `${theme}: reopen resets to overview`);
      await page.getByRole("button", { name: "Source", exact: true }).click();
      await page.getByRole("button", { name: "← files", exact: true }).click();
      await page.locator(".files-view").waitFor();
      await page.evaluate((revision) => {
        goToRoute(`file?${new URLSearchParams({ path: "review.mmd", revision })}`);
      }, revision);
      await page.locator(".is-interactive svg").waitFor();
      assertFits(await page.evaluate(measure), `${theme}: cached file opens as diagram again`);
      assert.deepEqual(errors, []);
      await context.close();
    }
    assert.deepEqual(writes, [], "view navigation never mutates project state or approves a run");
  } finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
});

test("Touch pinch/drag, zoom limits, and both ends of long diagrams remain reachable", async () => {
  const { server, base, writes } = await startServer();
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    await context.route("**/*", (route) => route.request().url().startsWith(base) ? route.continue() : route.abort());
    await context.addInitScript(() => {
      window.Clerk = { load: async () => {}, isSignedIn: true, user: { id: "member", firstName: "QA" }, session: { getToken: async () => "synthetic-test-only" } };
    });
    const page = await context.newPage();
    await page.goto(`${base}/#file?${new URLSearchParams({ path: "review.mmd", revision })}`);
    await page.getByRole("button", { name: "Actual size", exact: true }).click();
    const canvas = page.getByRole("region", { name: "Interactive diagram" });
    const before = await page.evaluate(measure);
    const cx = before.canvas.x + before.canvas.width / 2, cy = before.canvas.y + before.canvas.height / 2;
    const cdp = await context.newCDPSession(page);
    const touches = (distance, dy = 0) => [{ id: 1, x: cx - distance / 2, y: cy + dy }, { id: 2, x: cx + distance / 2, y: cy + dy }];
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: touches(80) });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: touches(120) });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    const pinched = await page.evaluate(measure);
    assert.ok(pinched.scale > before.scale * 1.4 && pinched.scale < before.scale * 1.6, "native pinch zooms the SVG");
    assert.equal(await page.evaluate(() => visualViewport.scale), 1, "pinch inside canvas leaves page zoom alone");
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ id: 1, x: cx, y: cy }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ id: 1, x: cx, y: cy - 80 }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    assert.ok((await page.evaluate(measure)).y < pinched.y - 60, "single-finger drag pans");
    assert.equal(await canvas.evaluate((node) => node.classList.contains("is-panning")), false);
    await cdp.detach();
    await canvas.focus();
    for (let i = 0; i < 12; i++) await page.keyboard.press("+");
    assert.equal((await page.evaluate(measure)).scale, 4);
    assert.equal(await page.getByRole("button", { name: "Zoom in", exact: true }).isEnabled(), false);
    for (let i = 0; i < 40; i++) await page.keyboard.press("-");
    assert.ok((await page.evaluate(measure)).scale > 0);
    assert.equal(await page.getByRole("button", { name: "Zoom out", exact: true }).isEnabled(), false);
    await page.getByRole("button", { name: "Fit diagram", exact: true }).click();
    assertFits(await page.evaluate(measure), "pinch/zoom-limit recovery");
    for (const name of ["wide.mmd", "tall.mmd"]) {
      await page.goto(`${base}/#file?${new URLSearchParams({ path: name, revision })}`);
      await page.getByRole("button", { name: "Actual size", exact: true }).click();
      const horizontal = name === "wide.mmd";
      for (const [delta, id] of [[1e6, "node_31"], [-1e6, "node_0"]]) {
        await canvas.dispatchEvent("wheel", { deltaX: horizontal ? delta : 0, deltaY: horizontal ? 0 : delta });
        const visible = await page.locator(`.node[data-id="${id}"]`).evaluate((node) => {
          const viewport = document.querySelector("[data-project-diagram]").getBoundingClientRect();
          const box = node.getBoundingClientRect();
          return box.left >= viewport.left && box.right <= viewport.right && box.top >= viewport.top && box.bottom <= viewport.bottom;
        });
        assert.ok(visible, `${name}: ${id} reachable at natural size without a bottom scrollbar`);
      }
    }
    assert.deepEqual(writes, []);
    await context.close();
  } finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
});
