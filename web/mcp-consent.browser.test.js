// Synthetic browser checks. Clerk still owns the real Allow/Deny behavior.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

test("consent preserves request, mounts Clerk only after sign-in, and recovers from load failure", async () => {
  const assets = path.resolve("src/tin_lite/static");
  const query = "client_id=client_test&scope=openid&state=a%2Bb%26c&redirect_uri=http%3A%2F%2F127.0.0.1%3A7777%2Fcallback&code_challenge=challenge&code_challenge_method=S256";
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname === "/mcp/consent") {
      let html = await fs.readFile(path.join(assets, "mcp-consent.html"), "utf8");
      const values = {
        ASSET_VERSION: "test", CLERK_FRONTEND_API_URL: "https://clerk.test",
        CLERK_PUBLISHABLE_KEY: "synthetic", SIGN_UP_URL: "https://accounts.test/sign-up",
        SIGN_IN_URL: "https://accounts.test/sign-in", PORTAL_CONSENT_URL: "https://accounts.test/oauth-consent",
        AGENT_PROMPT: "Use Tin to grow my project like a pro!",
        CONSENT_TITLE: "Let your coding agent grow your project.",
        CONSENT_TEXT: "Allow it to use your Tin account and it will plan your marketing from your codebase, run it on a schedule, and bring you drafts to approve.",
      };
      for (const [key, value] of Object.entries(values)) html = html.replaceAll(`{{${key}}}`, value);
      response.writeHead(200, {"Content-Type": "text/html", "Referrer-Policy": "strict-origin-when-cross-origin"});
      response.end(html);
    } else if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      const content = await fs.readFile(file).catch(() => null);
      if (!content) return response.writeHead(404).end();
      response.writeHead(200, {"Content-Type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"});
      response.end(content);
    } else response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    for (const mode of ["signed-in", "signed-out", "failed", "missing-component"]) for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
      await context.addInitScript(({mode, theme}) => {
        localStorage.setItem("tin-lite:theme", theme);
        window.consentCalls = [];
        window.Clerk = {
          isSignedIn: mode !== "signed-out",
          load: async () => { if (mode === "failed") throw new Error("provider detail must stay hidden"); },
          mountOAuthConsent: mode === "missing-component" ? undefined : (node, props) => {
            window.consentCalls.push({query: window.location.search, props});
            // Synthetic consent data/DOM for Tin styling checks. The official Clerk
            // component is responsible for producing these facts in production.
            node.innerHTML = `<div class="cl-rootBox"><div class="cl-cardBox"><section class="cl-card" style="display:flex;flex-direction:column">
              <header class="cl-header" style="display:flex;flex-direction:column;gap:8px">
                <img src="/assets/tin-logotype-ink.png" alt="Tin" width="31" height="20">
                <h2 class="cl-headerTitle" style="margin:8px 0 0">Coding agent · QA fixture</h2>
                <p class="cl-headerSubtitle" style="margin:0">Connect to Tin as qa@example.test</p>
              </header>
              <section style="font-size:14px;line-height:21px"><p style="margin:0 0 8px">This application requests access to:</p><ul style="margin:0;padding-left:20px"><li>Verify your identity (openid)</li></ul></section>
              <p style="margin:0;font-size:13px;line-height:19px;color:var(--ink-secondary)">The coding agent will access your Tin account. After your decision, you will return to <strong>127.0.0.1</strong>.</p>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px"><button type="button" name="consented" value="false">Deny</button><button type="button" name="consented" value="true">Allow</button></div>
            </section></div></div>`;
            for (const [name, style] of Object.entries(props.appearance.elements)) {
              for (const element of node.querySelectorAll(`.cl-${name}`)) for (const [key, value] of Object.entries(style)) if (typeof value !== "object") element.style[key] = value;
            }
          },
        };
      }, {mode, theme});
      const page = await context.newPage();
      await page.goto(`${base}/mcp/consent?${query}`);
      assert.equal(new URL(page.url()).search.slice(1), query);
      if (mode === "signed-in") {
        await page.locator("#clerk-oauth-consent").filter({hasText: "Coding agent · QA fixture"}).waitFor();
        const calls = await page.evaluate(() => window.consentCalls);
        assert.equal(calls.length, 1);
        assert.equal(calls[0].query, `?${query}`);
        assert.deepEqual(Object.keys(calls[0].props), ["appearance"]);
        assert.equal(await page.locator("#consent-sign-in").isVisible(), false);
        for (const name of ["Allow", "Deny"]) {
          const button = page.getByRole("button", {name, exact: true});
          assert.equal(await button.isVisible(), true);
          assert.equal(Math.round((await button.boundingBox()).height), 46);
        }
        for (const hidden of ["logoBox", "listGroup", "alert", "button"]) assert.equal(calls[0].props.appearance.elements[hidden], undefined);
      } else if (mode === "signed-out") {
        await page.locator("#consent-sign-in").waitFor();
        assert.equal(await page.evaluate(() => window.consentCalls.length), 0);
        // A returning founder whose session lives in another browser signs in; new ones create.
        const primary = page.locator("#consent-sign-in a.consent-create");
        assert.equal(await primary.innerText(), "Sign in");
        assert.equal(new URL(await primary.getAttribute("href")).pathname, "/sign-in");
        const create = page.getByRole("link", {name: "Create an account", exact: true});
        assert.equal(new URL(await create.getAttribute("href")).pathname, "/sign-up");
      } else {
        await page.getByRole("status").filter({hasText: "could not load"}).waitFor();
        assert.equal(await page.evaluate(() => window.consentCalls.length), 0);
        assert.equal((await page.locator("body").innerText()).includes("provider detail"), false);
      }
      // The page carries the aspirational headline and no "Then, back in your project" section (Emre).
      assert.equal(await page.getByRole("heading", {name: "Let your coding agent grow your project.", exact: true}).isVisible(), true);
      assert.equal((await page.locator("body").innerText()).includes("Use Tin to grow my project like a pro!"), false);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
      if (process.env.TIN_AUTH_SCREENSHOTS) {
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({path: `${process.env.TIN_AUTH_SCREENSHOTS}/consent-${mode}-${theme}-mobile.png`, fullPage: true});
        await page.setViewportSize({width: 1440, height: 1000});
        await page.screenshot({path: `${process.env.TIN_AUTH_SCREENSHOTS}/consent-${mode}-${theme}.png`, fullPage: true});
      }
      await context.close();
    }
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
