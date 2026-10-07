// The reader's page bundles: figures, embeds and videos from a draft, in a real browser.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const folder = "content/articles/2026-10-05-1a2b3c4d.assets";
const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#c33"/></svg>';
// The embed tries everything it must not be able to do, then reports back.
const embed = `<!doctype html><html><body><p id="state">ready</p><script>
  const attempt = (fn) => { try { return String(fn()); } catch (error) { return "denied"; } };
  const result = {
    parent: attempt(() => parent.document.title),
    cookie: attempt(() => document.cookie),
    storage: attempt(() => localStorage.length),
  };
  new Image().src = "${"{ORIGIN}"}/track-image";
  fetch("${"{ORIGIN}"}/track-fetch").then(() => "allowed", () => "blocked").then((fetched) => {
    parent.postMessage({ embed: { ...result, fetched } }, "*");
  });
</script></body></html>`;
// A tall piece that reports the theme Tin gives it, now and after a switch.
const tall = `<!doctype html><html><body style="margin:0"><div style="height:700px"></div><script>
  const say = () => parent.postMessage({ tall: document.documentElement.dataset.theme }, "*");
  say();
  new MutationObserver(say).observe(document.documentElement, { attributes: true });
</script></body></html>`;

function documentData() {
  return {
    filename: "2026-10-05-1a2b3c4d.md",
    word_count: 40,
    reading_minutes: 1,
    html: [
      "<h1>Filming the trailers</h1>",
      `<p><img class="md-asset" data-asset="${folder}/flow.svg" alt="How a shot is made" data-fallback-name="flow.svg" title="Each shot follows recorded game events" /></p>`,
      `<figure class="md-embed" data-asset="${folder}/field.html" data-height="300" data-title="Move the dog"></figure>`,
      `<figure class="md-embed" data-asset="${folder}/tall.html" data-height="300" data-title="A tall piece"></figure>`,
      `<figure class="md-video" data-url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" data-title="The standoff"><a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ" rel="noreferrer">The standoff</a></figure>`,
      `<figure class="md-video" data-url="https://media.sheepdogs.io/trailers/sheepdogs-standoff.mp4" data-title="The film"><a href="https://media.sheepdogs.io/trailers/sheepdogs-standoff.mp4" rel="noreferrer">The film</a></figure>`,
      `<figure class="md-video" data-url="https://example.com/watch/1" data-title="Elsewhere"><a href="https://example.com/watch/1" rel="noreferrer">Elsewhere</a></figure>`,
      `<p><img class="md-asset" data-asset="${folder}/missing.svg" alt="Left out" data-fallback-name="missing.svg" /></p>`,
      `<aside class="md-callout" data-kind="tip"><p class="md-callout-label">Tip</p><p>Keep the dog behind the flock.</p></aside>`,
    ].join("\n"),
    assets: [
      { path: `${folder}/flow.svg`, media_type: "image/svg+xml" },
      { path: `${folder}/field.html`, media_type: "text/html" },
      { path: `${folder}/tall.html`, media_type: "text/html" },
    ],
    asset_notes: ["unsafe.svg was left out: the SVG contains script, event handlers or outside references."],
  };
}

test("page bundles: figures load inert, embeds are walled off, videos are allow-listed", async () => {
  const requests = [];
  const server = http.createServer(async (request, response) => {
    requests.push(request.url);
    if (request.url.startsWith("/assets/")) {
      const file = path.resolve(assets, request.url.slice(8));
      if (!file.startsWith(`${assets}/`)) return response.writeHead(404).end();
      response.setHeader("Content-Type", file.endsWith(".css") ? "text/css" : "text/javascript");
      return response.end(await fs.readFile(file));
    }
    if (request.url === "/") {
      response.setHeader("Content-Type", "text/html");
      return response.end(
        '<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/assets/app.css"></head>' +
        '<body><main id="main"></main><script src="/assets/markdown-viewer.js"></script></body></html>',
      );
    }
    response.writeHead(404).end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext();
    // No real network: providers are recorded, not loaded.
    const external = [];
    await context.route(/^https:\/\//, (route) => { external.push(route.request().url()); return route.abort(); });
    const page = await context.newPage();
    await page.goto(origin);
    await page.evaluate(({ data, files }) => {
      document.cookie = "tin_session=secret";
      localStorage.setItem("tin", "secret");
      window.embedReports = [];
      window.addEventListener("message", (event) => window.embedReports.push(event.data));
      const encoder = new TextEncoder();
      window.TinMarkdownViewer.mount(document.getElementById("main"), data, {
        mode: "in-app",
        loadAsset: async (assetPath) => {
          if (!(assetPath in files)) throw new Error("missing");
          return encoder.encode(files[assetPath]).buffer;
        },
      });
    }, {
      data: documentData(),
      files: {
        [`${folder}/flow.svg`]: svg,
        [`${folder}/field.html`]: embed.replaceAll("{ORIGIN}", origin),
        [`${folder}/tall.html`]: tall,
      },
    });

    // The figure becomes an inert data: image with its caption; a left-out one shows its name.
    const figure = page.locator("figure.md-figure img");
    await figure.waitFor();
    await page.waitForFunction(() => document.querySelector("figure.md-figure img")?.src.startsWith("data:image/svg+xml"));
    assert.equal(await page.locator("figure.md-figure figcaption").first().textContent(), "Each shot follows recorded game events");
    assert.match(await page.locator(".md-image-fallback").textContent(), /missing\.svg/);
    assert.match(await page.locator(".md-asset-notes").textContent(), /unsafe\.svg was left out/);
    assert.ok(await page.evaluate(() => document.querySelector("h1").nextElementSibling.matches(".md-asset-notes")));

    // The embed runs, but in an opaque origin with no network.
    const frame = page.locator('figure[data-title="Move the dog"] iframe.md-embed-frame');
    await frame.waitFor();
    assert.equal(await frame.getAttribute("sandbox"), "allow-scripts");
    assert.match(await frame.getAttribute("srcdoc"), /Content-Security-Policy/);
    await page.waitForFunction(() => window.embedReports.some((data) => data.embed));
    const { embed: report } = await page.evaluate(() => window.embedReports.find((data) => data.embed));
    assert.deepEqual(report, { parent: "denied", cookie: "denied", storage: "denied", fetched: "blocked" });
    assert.ok(!requests.some((url) => url.startsWith("/track")), `the embed reached the server: ${requests}`);

    // No card around a piece, and its frame grows to the piece's own height.
    const tallFrame = page.locator('figure[data-title="A tall piece"] iframe.md-embed-frame');
    await page.waitForFunction(() => document.querySelector('figure[data-title="A tall piece"] iframe')?.offsetHeight === 700);
    assert.deepEqual(await tallFrame.evaluate((node) => {
      const style = getComputedStyle(node);
      return [style.borderTopWidth, style.backgroundColor, style.colorScheme];
    }), ["0px", "rgba(0, 0, 0, 0)", "light"]);
    // The piece takes the reader's theme, and follows a switch.
    await page.waitForFunction(() => window.embedReports.some((data) => data.tall === "light"));
    await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
    await page.waitForFunction(() => window.embedReports.some((data) => data.tall === "dark"));
    assert.equal(await tallFrame.evaluate((node) => getComputedStyle(node).colorScheme), "dark");
    assert.equal(await figure.evaluate((node) => getComputedStyle(node).colorScheme), "dark");
    await page.evaluate(() => { delete document.documentElement.dataset.theme; });

    // Videos: an allow-listed provider plays in its privacy mode, a file loads only on play,
    // and anything else stays a link.
    const youtube = page.locator("iframe.md-video-frame");
    assert.equal(await youtube.getAttribute("src"), "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ");
    const film = page.locator("video.md-video-file");
    assert.equal(await film.getAttribute("preload"), "none");
    assert.equal(await page.locator('figure.md-video a[href="https://example.com/watch/1"]').count(), 1);
    assert.ok(!external.some((url) => url.includes("sheepdogs-standoff.mp4")), "the film must not preload");

    // The callout reads as a labelled aside.
    assert.equal(await page.locator(".md-callout .md-callout-label").textContent(), "Tip");
    if (process.env.TIN_BUNDLE_SCREENSHOTS) {
      await page.screenshot({ path: `${process.env.TIN_BUNDLE_SCREENSHOTS}/page-bundle.png`, fullPage: true });
    }
  } finally {
    await browser.close();
    server.close();
  }
});
