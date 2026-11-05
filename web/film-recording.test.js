// A site's recording: answers saved once and replayed strictly, outside effects never sent live,
// trackers answered empty, and no secret kept.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {findSecrets, openRecording} from "./film/recording.js";

// A request as Playwright's router hands it over, and what the router did with it.
function routed(method, url, body = null, live = {status: 200, body: `answer for ${url}`, headers: {"content-type": "text/html", "set-cookie": "sid=s3cret-session", "x-frame-options": "DENY"}}) {
  const done = {};
  return {
    done,
    route: {
      request: () => ({method: () => method, url: () => url, postData: () => body}),
      fetch: async () => {
        done.fetched = true;
        return {status: () => live.status, headers: () => live.headers, body: async () => Buffer.from(live.body)};
      },
      fulfill: async answer => { done.fulfilled = answer; },
      abort: async () => { done.aborted = true; },
    },
  };
}

test("a recording saves what the live site answered and replays it, refusing what it never saw", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "film-recording-"));
  const live = openRecording(dir, {record: true});
  const page = routed("GET", "https://example.com/");
  await live.route(page.route);
  assert.ok(page.done.fetched);
  assert.equal(page.done.fulfilled.body.toString(), "answer for https://example.com/");
  // Cookies and framing rules never reach the saved answer.
  assert.deepEqual(page.done.fulfilled.headers, {"content-type": "text/html"});
  assert.deepEqual(live.close(), {added: 1, entries: 1, misses: [], stopped: []});

  const replay = openRecording(dir);
  const again = routed("GET", "https://example.com/");
  await replay.route(again.route);
  assert.ok(!again.done.fetched, "a replay never goes to the live site");
  assert.equal(again.done.fulfilled.body.toString(), "answer for https://example.com/");
  const unseen = routed("GET", "https://example.com/pricing");
  await replay.route(unseen.route);
  assert.ok(unseen.done.aborted && !unseen.done.fetched);
  assert.deepEqual(replay.close().misses, ["GET https://example.com/pricing"]);
});

test("recording never sends an outside effect, sends only the reads it's told of, and answers trackers empty", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "film-recording-"));
  const live = openRecording(dir, {record: true, reads: ["/graphql$"], answer: (method, url) => (url.endsWith("/staged") ? {status: 200, body: "{\"sent\":true}"} : undefined)});
  const send = routed("POST", "https://example.com/api/invoices/send", "{\"all\":true}");
  const query = routed("POST", "https://example.com/graphql", "{\"query\":\"{ invoices }\"}");
  const staged = routed("POST", "https://example.com/staged", "{}");
  const tracker = routed("GET", "https://edge.tin.computer/sdk/v1.js");
  const reporter = routed("POST", "https://example.com/ingest/batch/", "[]");
  for (const item of [send, query, staged, tracker, reporter]) await live.route(item.route);
  assert.ok(send.done.aborted && !send.done.fetched, "a send is never sent");
  assert.ok(query.done.fetched, "a named read goes out and is saved");
  assert.equal(staged.done.fulfilled.body, "{\"sent\":true}");
  assert.ok(!staged.done.fetched);
  for (const item of [tracker, reporter]) assert.equal(item.done.fulfilled.status, 204);
  const closed = live.close();
  assert.equal(closed.stopped.length, 1);
  assert.match(closed.stopped[0], /^POST https:\/\/example\.com\/api\/invoices\/send [0-9a-f]{12}$/);
  assert.equal(closed.entries, 1);
});

test("a secret in any saved answer is found by its address", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "film-recording-"));
  const live = openRecording(dir, {record: true});
  await live.route(routed("GET", "https://example.com/me", null, {status: 200, body: "{\"token\":\"tok_live_8f2c91aa\"}", headers: {"content-type": "application/json"}}).route);
  await live.route(routed("GET", "https://example.com/").route);
  live.close();
  assert.deepEqual(findSecrets(dir, ["tok_live_8f2c91aa"]), ["GET https://example.com/me"]);
  assert.deepEqual(findSecrets(dir, ["s3cret-session"]), [], "a dropped cookie is never saved");
  assert.deepEqual(findSecrets(dir, ["short"]), [], "too short to look for");
});
