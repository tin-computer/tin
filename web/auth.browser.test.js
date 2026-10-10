// Real packaged shell and Clerk mount contract; no real identity, email or token.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";

const assets = path.resolve("src/tin_lite/static");
const continuation = "https://clerk.tin.computer/oauth/authorize/continue?client_id=codex&state=a%2Bb%26c&code_challenge=opaque&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A7777%2Fcallback";
const escaped = value => value.replaceAll("&", "&amp;").replaceAll('"', "&quot;");

test("auth keeps the OAuth return through both account modes and bypasses provisioning for signed-in users", async () => {
  const apis = [], errors = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (["/", "/sign-in", "/sign-up"].includes(url.pathname)) {
      let html = await fs.readFile(path.join(assets, "index.html"), "utf8");
      const returning = url.searchParams.get("redirect_url") || "";
      for (const [key, value] of Object.entries({ASSET_VERSION: "test", BILLING_ENABLED: "false", CLERK_PUBLISHABLE_KEY: "synthetic", CLERK_FRONTEND_API_URL: "https://clerk.test", AUTH_RETURN_URL: escaped(returning), AUTH_FLOW: returning ? "mcp" : "product"})) html = html.replaceAll(`{{${key}}}`, value);
      response.writeHead(200, {"Content-Type": "text/html"});
      return response.end(html);
    }
    if (url.pathname.startsWith("/api/")) {apis.push(url.pathname); response.writeHead(500).end(); return;}
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      const content = await fs.readFile(file).catch(() => null);
      if (!content) return response.writeHead(404).end();
      response.writeHead(200, {"Content-Type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "application/octet-stream"});
      return response.end(content);
    }
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    for (const mode of ["sign-in", "sign-up"]) for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
      await context.addInitScript(theme => {
        localStorage.setItem("tin-lite:theme", theme);
        window.authCalls = [];
        window.Clerk = {
          isSignedIn: false,
          load: async props => {window.loadProps = props;},
          mountSignIn: (node, props) => {window.authCalls.push({mode: "sign-in", props}); node.textContent = "Sign-in fixture";},
          mountSignUp: (node, props) => {window.authCalls.push({mode: "sign-up", props}); node.textContent = "Sign-up fixture";},
        };
      }, theme);
      const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
      await page.goto(`${base}/${mode}?${new URLSearchParams({redirect_url: continuation})}`);
      await page.locator("#clerk-auth").waitFor();
      const calls = await page.evaluate(() => window.authCalls);
      assert.equal(calls.length, 1); assert.equal(calls[0].mode, mode);
      assert.equal(calls[0].props.forceRedirectUrl, continuation);
      assert.deepEqual(await page.evaluate(() => window.loadProps.allowedRedirectOrigins), ["https://clerk.tin.computer"]);
      assert.equal(calls[0].props.fallbackRedirectUrl, continuation);
      const other = mode === "sign-in" ? "signUpUrl" : "signInUrl";
      assert.equal(new URL(calls[0].props[other], base).searchParams.get("redirect_url"), continuation);
      assert.equal(await page.evaluate(() => window.loadProps.localization.signIn.start.subtitle), "Connect your coding agent to your Tin account.");
      assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), theme);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      // A normal product sign-in still uses the normal destination and copy.
      await page.goto(`${base}/${mode}?invite=invitation`);
      await page.locator("#clerk-auth").waitFor();
      const ordinary = await page.evaluate(() => window.authCalls[0].props);
      assert.equal(ordinary.forceRedirectUrl, undefined);
      assert.equal(ordinary.fallbackRedirectUrl, "/system?invite=invitation");
      assert.equal(ordinary.routing, "path");
      assert.equal(ordinary.path, `/${mode}`);
      await context.close();
    }
    const context = await browser.newContext();
    let returned;
    await context.route("**/*", route => {
      const url = route.request().url();
      if (url.startsWith(base)) return route.continue();
      if (url === continuation) {returned = url; return route.fulfill({contentType: "text/html", body: "OAuth resumed"});}
      return route.abort();
    });
    await context.addInitScript(() => {window.Clerk = {isSignedIn: true, load: async () => {}};});
    const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${base}/sign-in?${new URLSearchParams({redirect_url: continuation})}`);
    await page.getByText("OAuth resumed", {exact: true}).waitFor();
    assert.equal(returned, continuation);
    assert.deepEqual(apis, []); // No redundant personal project created during OAuth.
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});

test("Clerk verification navigation retains the mounted flow and leaves auth with a document navigation", async () => {
  const documents = [], apis = [], errors = [];
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    if (/^\/sign-(in|up)(\/|$)/.test(url.pathname)) {
      documents.push(`${url.pathname}${url.search}`);
      let html = await fs.readFile(path.join(assets, "index.html"), "utf8");
      for (const [key, value] of Object.entries({ASSET_VERSION: "test", BILLING_ENABLED: "false", CLERK_PUBLISHABLE_KEY: "synthetic", CLERK_FRONTEND_API_URL: "https://clerk.test", AUTH_RETURN_URL: escaped(continuation), AUTH_FLOW: "mcp"})) html = html.replaceAll(`{{${key}}}`, value);
      response.writeHead(200, {"Content-Type": "text/html"}); return response.end(html);
    }
    if (url.pathname === "/system") {documents.push(request.url); response.writeHead(200, {"Content-Type": "text/html"}); return response.end("Signed-in destination");}
    if (url.pathname.startsWith("/api/")) {apis.push(url.pathname); response.writeHead(500).end(); return;}
    if (url.pathname.startsWith("/assets/")) {
      const content = await fs.readFile(path.join(assets, url.pathname.slice(8))).catch(() => null);
      if (!content) return response.writeHead(404).end();
      response.writeHead(200, {"Content-Type": url.pathname.endsWith(".js") ? "text/javascript" : url.pathname.endsWith(".css") ? "text/css" : "application/octet-stream"}); return response.end(content);
    }
    response.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    for (const mode of ["sign-in", "sign-up"]) {
      documents.length = 0;
      const context = await browser.newContext();
      await context.route("**/*", route => route.request().url().startsWith(base) ? route.continue() : route.abort());
      await context.addInitScript(() => {
        window.Clerk = {
          isSignedIn: false, load: async props => {window.loadProps = props;},
          mountSignIn: node => {node.innerHTML = '<input aria-label="Code" value="123456">';},
          mountSignUp: node => {node.innerHTML = '<input aria-label="Code" value="123456">';},
        };
      });
      const page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
      const entry = `/${mode}?${new URLSearchParams({redirect_url: continuation})}`;
      await page.goto(base + entry);
      const code = page.getByRole("textbox", {name: "Code"}); await code.waitFor();
      const initialHistory = await page.evaluate(() => history.length);
      const step = `/${mode}/factor-one?${new URLSearchParams({redirect_url: continuation})}`;
      await page.evaluate(to => window.loadProps.routerPush(to), step);
      await page.waitForURL(base + step);
      assert.equal(await code.inputValue(), "123456");
      assert.equal(await page.evaluate(() => history.length), initialHistory + 1);
      assert.deepEqual(documents, [entry]); // No new document or verification remount.
      const replacement = step + "&step=verification";
      await page.evaluate(to => window.loadProps.routerReplace(to), replacement);
      assert.equal(page.url(), base + replacement);
      assert.equal(await page.evaluate(() => history.length), initialHistory + 1);
      await page.goBack(); assert.equal(page.url(), base + entry);
      await page.goForward(); assert.equal(page.url(), base + replacement);
      assert.equal(await code.inputValue(), "123456");
      assert.deepEqual(documents, [entry]);
      assert.deepEqual(apis, []); // Auth history must not start dashboard work.
      // Changing modes must bootstrap the other Clerk component.
      const other = mode === "sign-in" ? "/sign-up" : "/sign-in";
      await page.evaluate(to => window.loadProps.routerPush(to), other);
      await page.waitForURL(base + other); await code.waitFor();
      assert.deepEqual(documents, [entry, other]);
      // Successful authentication must bootstrap the product, not just change its URL.
      await page.evaluate(() => window.loadProps.routerReplace("/system?project=synthetic"));
      await page.getByText("Signed-in destination").waitFor();
      assert.equal(page.url(), base + "/system?project=synthetic");
      await context.close();
    }
    assert.deepEqual(errors, []);
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
});
