// The packaged dashboard on a recorded world: the dashboard's own answers for one demo project,
// replayed by address, with a film's staged changes on top. There is no database and no account.
// A world file holds every answer the dashboard asked for while the project was open, scrubbed
// of member IDs, the workspace's name and its billing. A world recorded from a live project stays
// out of the repository, in .films/worlds/ (which git ignores); a made-up one can live in
// web/film/worlds/. `stage` lets a
// film change answers (`answer(method, url, body)` returns {status, json} or nothing) and see what
// the dashboard sent (`writes`).
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";

const assets = path.resolve("src/tin_lite/static");
const TYPES = {".js": "text/javascript", ".css": "text/css", ".wasm": "application/wasm", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json"};

export async function loadWorld(name) {
  for (const folder of [".films/worlds", "web/film/worlds"]) {
    try {
      return JSON.parse(await fs.readFile(path.resolve(folder, `${name}.json`), "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  }
  throw new Error(`No world called ${name}: a recorded world goes in .films/worlds/${name}.json, which stays out of git.`);
}

export async function serveReplay({world, answer = () => undefined}) {
  const recorded = new Map(Object.entries(world.responses));
  // An address asked with other query values still finds its page's answer.
  const byPath = new Map();
  for (const key of recorded.keys()) if (!byPath.has(key.split("?")[0])) byPath.set(key.split("?")[0], key);
  const writes = [];
  const misses = new Set();
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, "http://localhost");
    const reply = (status, value, type = "application/json") => {
      response.statusCode = status;
      response.setHeader("Content-Type", type);
      response.end(typeof value === "string" ? value : JSON.stringify(value));
    };
    if (url.pathname.startsWith("/assets/")) {
      const file = path.join(assets, url.pathname.slice(8));
      try {
        const body = await fs.readFile(file);
        response.setHeader("Content-Type", TYPES[path.extname(file)] || "application/octet-stream");
        return response.end(body);
      } catch {
        return reply(404, "", "text/plain");
      }
    }
    if (!url.pathname.startsWith("/api/")) {
      const html = (await fs.readFile(path.join(assets, "index.html"), "utf8"))
        .replaceAll("{{ASSET_VERSION}}", "film").replaceAll("{{CLERK_PUBLISHABLE_KEY}}", "")
        .replaceAll("{{BROWSER_LOCK_ENABLED}}", "false").replaceAll("{{MCP_URL}}", "https://app.tin.computer/mcp")
        .replaceAll("{{AUTH_RETURN_URL}}", "").replaceAll("{{AUTH_FLOW}}", "product");
      return reply(200, html, "text/html");
    }
    let body = null;
    if (request.method !== "GET" && request.method !== "HEAD") {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const text = Buffer.concat(chunks).toString("utf8");
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      writes.push({method: request.method, url: url.pathname + url.search, body});
    }
    const staged = await answer(request.method, url, body);
    if (staged) return reply(staged.status || 200, staged.json ?? staged.text ?? "", staged.text !== undefined ? "text/plain" : "application/json");
    if (request.method !== "GET") return reply(204, "", "text/plain");
    const key = url.pathname + url.search;
    const found = recorded.get(key) || recorded.get(byPath.get(url.pathname));
    if (found) return reply(found.status, found.json !== undefined ? found.json : found.text, found.type || "application/json");
    misses.add(key);
    return reply(200, url.pathname.endsWith("s") ? [] : {});
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, writes, misses, base: `http://127.0.0.1:${server.address().port}`};
}
