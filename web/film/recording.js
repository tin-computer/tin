// A recorded world for a live site: every answer its pages ask for, saved while the film plays
// against the live site once (`record`), then served from disk for every take, so takes are the
// same frame for frame and never touch the live site again.
//
// - Replay is strict: a request the recording doesn't hold is refused and named, so a gap fails
//   the rehearsal instead of reaching the screen as an empty pane.
// - Recording is read-only for outside effects: a request that isn't a GET goes out only when the
//   film names it as a read (`reads`, such as a search or a GraphQL query). Anything else (a send,
//   a publish, a payment) is stopped and named; the film stages its answer (`answer`) instead.
// - A recording keeps no secrets: request headers are never saved, a response's cookies and
//   tracking headers are dropped, and `secrets` (a test account's password, a session's token) are
//   looked for in every saved body, refusing the recording when one is there.
//
// On disk, `<dir>/index.json` lists the answers by method and address, and `<dir>/bodies/` holds
// each body once, by its SHA-1.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

// What only measures visits, never part of a film.
// Tin Analytics reports a founder's site through the site's own /ingest/ path.
const TRACKERS = /vercel-insights|_vercel\/insights|_vercel\/speed-insights|posthog|plausible|google-analytics|googletagmanager|edge\.tin\.computer|tin\.computer\/(?:sdk|edge)|\/ingest\/|segment\.io|hotjar|clarity\.ms|doubleclick|facebook\.net/;
// Headers a recording leaves out: cookies, and what would stop the page being framed or decoded
// again from disk.
const DROPPED = /^(set-cookie|x-frame-options|content-security-policy|content-security-policy-report-only|content-encoding|content-length|transfer-encoding|strict-transport-security|report-to|nel|alt-svc)$/i;

const sha1 = value => crypto.createHash("sha1").update(value).digest("hex");

export function recordingKey(method, url, body = null) {
  return method === "GET" || method === "HEAD" ? `${method} ${url}` : `${method} ${url} ${sha1(body ?? "").slice(0, 12)}`;
}

export function openRecording(dir, {record = false, reads = [], answer = () => undefined} = {}) {
  const file = path.join(dir, "index.json");
  const index = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {version: 1, entries: {}};
  const misses = new Set();
  const stopped = new Set();
  let added = 0;
  const read = url => reads.some(pattern => new RegExp(pattern).test(url));

  const route = async route => {
    const request = route.request();
    const method = request.method();
    const url = request.url();
    // A tracker gets an empty answer, so the page has no failed load to report.
    if (TRACKERS.test(url)) return route.fulfill({status: 204, body: ""});
    const body = request.postData();
    const staged = await answer(method, url, body);
    if (staged) return route.fulfill(staged);
    const key = recordingKey(method, url, body);
    const entry = index.entries[key];
    if (entry) return route.fulfill({status: entry.status, headers: entry.headers, body: fs.readFileSync(path.join(dir, "bodies", entry.body))});
    if (!record) {
      misses.add(key);
      return route.abort();
    }
    if (method !== "GET" && method !== "HEAD" && !read(url)) {
      stopped.add(key);
      return route.abort();
    }
    let response;
    try {
      response = await route.fetch();
    } catch {
      misses.add(key);
      return route.abort();
    }
    const headers = Object.fromEntries(Object.entries(response.headers()).filter(([name]) => !DROPPED.test(name)));
    const bytes = await response.body();
    const hash = sha1(bytes);
    fs.mkdirSync(path.join(dir, "bodies"), {recursive: true});
    fs.writeFileSync(path.join(dir, "bodies", hash), bytes);
    index.entries[key] = {method, url, status: response.status(), headers, body: hash, bytes: bytes.length};
    added++;
    return route.fulfill({status: response.status(), headers, body: bytes});
  };

  // Write the index (a recording take only) and say what the take lacked or stopped.
  const close = () => {
    if (record && added) {
      fs.mkdirSync(dir, {recursive: true});
      fs.writeFileSync(file, `${JSON.stringify(index, null, 1)}\n`);
    }
    return {added, entries: Object.keys(index.entries).length, misses: [...misses], stopped: [...stopped]};
  };
  return {route, close};
}

// What a recording must not hold: each secret's exact text in any saved body. Returns the
// addresses whose answers hold one, empty when the recording is clean.
export function findSecrets(dir, secrets) {
  const wanted = secrets.filter(secret => typeof secret === "string" && secret.length >= 6).map(secret => Buffer.from(secret));
  if (!wanted.length) return [];
  const file = path.join(dir, "index.json");
  if (!fs.existsSync(file)) return [];
  const {entries} = JSON.parse(fs.readFileSync(file, "utf8"));
  return Object.values(entries)
    .filter(entry => {
      const body = fs.readFileSync(path.join(dir, "bodies", entry.body));
      return wanted.some(secret => body.includes(secret)) || Object.values(entry.headers).some(value => wanted.some(secret => String(value).includes(secret.toString())));
    })
    .map(entry => `${entry.method} ${entry.url}`);
}
