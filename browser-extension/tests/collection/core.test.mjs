import assert from "node:assert/strict";
import test from "node:test";
import "../../src/collection/core.js";
import { selection, snapshot, person, searchURL, actor } from "./fixtures.mjs";

const C = globalThis.TinCollectorCore;
const now = Date.parse("2026-01-01T00:00:00Z");
const run = () => C.createRun(selection, now, "test-run");
const accept = (state, page) => C.acceptSnapshot(state, page, now + 1000);

test("normalizes profile URLs and rejects foreign origins and ambiguous filters", () => {
  assert.equal(C.profileURL("https://linkedin.com/in/test-person-1/?trk=abc#x"), person(1).profile_url);
  for (const url of ["http://www.linkedin.com/in/x", "https://www.linkedin.com.evil.test/in/x", "https://x@www.linkedin.com/in/x", "https://www.linkedin.com/in/a%2Fb", "https://www.linkedin.com/company/x", "https://www.linkedin.com/in/" + "a".repeat(5000)]) assert.throws(() => C.profileURL(url));
  for (const suffix of ["&page=0", "&page=2&page=3", '&connectionOf=["other"]']) assert.throws(() => C.searchContext(searchURL() + suffix));
  assert.equal(C.searchContext(searchURL() + "&sid=session&trk=foo&origin=bar").url, searchURL());
  assert.throws(() => C.createRun({ ...selection, collection_url: searchURL(2) }), /start_at_first_page/);
});

test("deduplicates across pages, records provenance and accepts second-degree people only", () => {
  const a = accept(run(), snapshot(1, [person(1), person(1), person(2, "1st"), person(3, "unknown")]));
  const b = accept(a, snapshot(2, [person(1), person(4), { ...person(5), profile_url: actor.profile_url }, { ...person(6), profile_url: selection.friend.profile_url }], "end"));
  assert.deepEqual(b.people.map(p => p.profile_url), [person(1).profile_url, person(4).profile_url]);
  assert.deepEqual(b.skipped, { first_degree: 1, unknown_degree: 1, invalid: 0 });
  assert.equal(b.status, "completed");
  assert.equal(b.completeness, "visible_results_only");
  const result = C.result(b);
  assert.equal(result.paths[1].friend_profile_url, selection.friend.profile_url);
  assert.equal(result.paths[1].source_page, 2);
  assert.equal(result.paths[1].actor_key, actor.key);
  assert.equal(result.coverage.pages[0].fingerprint, undefined);
});

test("new collections request second-degree results and keep optional search separate from other filters", () => {
  const original = { ...selection, collection_url: selection.collection_url + '&network=%5B%22F%22%2C%22S%22%5D&geoUrn=%5B%22test-location%22%5D&page=4&keywords=old' };
  const before = structuredClone(original);
  const prepared = C.prepareSelection(original);
  const url = new URL(prepared.collection_url);
  assert.deepEqual(JSON.parse(url.searchParams.get("network")), ["S"]);
  assert.equal(url.searchParams.has("keywords"), false);
  assert.equal(C.searchContext(url.href).page, 1);
  assert.equal(C.searchContext(url.href).friend_id, "test-friend-id");
  assert.equal(url.searchParams.get("geoUrn"), '["test-location"]');
  assert.deepEqual(original, before);
  const keywords = 'designer OR "software engineer" &network=["F"]';
  const searched = new URL(C.prepareSelection(original, ` ${keywords} `).collection_url);
  assert.equal(searched.searchParams.get("keywords"), keywords);
  assert.deepEqual(searched.searchParams.getAll("network"), ['["S"]']);
  assert.equal(new URL(C.prepareSelection(original, "  ").collection_url).searchParams.has("keywords"), false);
  for (const invalid of [null, {}, 1, "a".repeat(301), "two\nlines", "null\u0000byte"]) assert.throws(() => C.prepareSelection(selection, invalid), /invalid_keywords/);
});

test("second-degree scope and keyword search stay pinned through Next, replay and export", () => {
  const keywords = 'designer OR "software engineer"';
  const prepared = C.prepareSelection(selection, keywords);
  const first = { ...snapshot(), url: prepared.collection_url };
  const a = accept(C.createRun(prepared, now), first);
  const nextURL = new URL(prepared.collection_url);
  nextURL.searchParams.set("page", "2");
  nextURL.searchParams.set("connectionOf", JSON.stringify("test-friend-id"));
  nextURL.searchParams.set("spellCorrectionEnabled", "true");
  nextURL.searchParams.set("prioritizeMessage", "false");
  const second = { ...snapshot(2, [person(2)], "end"), url: nextURL.href };
  const b = accept(a, second);
  assert.equal(b.pages.length, 2);
  assert.deepEqual(accept(b, second), b);
  assert.deepEqual(C.result(b).filters, { network: ["S"], keywords });
  assert.ok(C.csv(b).includes('"search_query"'));
  assert.ok(C.csv(b).includes('"designer OR ""software engineer"""'));
  for (const [key, value] of [["network", '["F","S"]'], ["keywords", "changed"]]) {
    const changed = new URL(second.url); changed.searchParams.set(key, value);
    assert.throws(() => accept(a, { ...second, url: changed.href }), /filters_changed/);
  }
  const removed = new URL(second.url); removed.searchParams.delete("keywords");
  assert.throws(() => accept(a, { ...second, url: removed.href }), /filters_changed/);
  const legacy = run(); delete legacy.filters;
  assert.deepEqual(C.result(legacy).filters, { network: null, keywords: "" });
  assert.equal(accept(legacy, snapshot()).pages.length, 1);
});

test("checkpoint replay is idempotent but changed, skipped and repeated pages fail closed", () => {
  const a = accept(run(), snapshot(1, [person(1), person(2)]));
  assert.deepEqual(accept(a, snapshot(1, [person(2), person(1)])), a);
  assert.throws(() => accept(a, snapshot(1, [person(3)])), /page_changed/);
  assert.throws(() => accept(a, snapshot(3, [person(3)])), /page_mismatch/);
  const repeated = accept(a, snapshot(2, [person(2), person(1)]));
  assert.equal(repeated.reason, "repeated_page");
  assert.equal(repeated.pages.length, 1);
  assert.equal(repeated.completeness, "partial");
});

test("checks actor, friend and other selected filters on every page", () => {
  const a = run();
  assert.throws(() => accept(a, { ...snapshot(), actor: { key: "other" } }), /account_changed/);
  for (const url of [searchURL(1, "other-friend"), searchURL() + "&keywords=changed"]) assert.throws(() => accept(a, { ...snapshot(), url }), /filters_changed/);
  assert.throws(() => accept(a, { ...snapshot(), page: 2 }), /page_mismatch/);
  assert.throws(() => accept(a, snapshot(1, Array.from({ length: 501 }, (_, i) => person(i)))), /invalid_snapshot/);
});

test("Next may reserialize a single friend and add default UI flags without changing the selection", () => {
  const a = accept(run(), snapshot());
  const nextURL = new URL(searchURL(2));
  nextURL.searchParams.set("connectionOf", JSON.stringify("test-friend-id"));
  nextURL.searchParams.set("spellCorrectionEnabled", "true");
  nextURL.searchParams.set("prioritizeMessage", "false");
  const next = { ...snapshot(2, [person(2)]), url: nextURL.href };
  assert.equal(accept(a, next).pages.length, 2);
  nextURL.searchParams.set("prioritizeMessage", "true");
  assert.throws(() => accept(a, { ...next, url: nextURL.href }), /filters_changed/);
  nextURL.searchParams.set("connectionOf", JSON.stringify(["test-friend-id", "another-friend"]));
  assert.throws(() => C.searchContext(nextURL.href), /ambiguous_friend_filter/);
});

test("distinguishes confirmed empty results, unreadable pages and missing pagination", () => {
  assert.throws(() => accept(run(), snapshot(1, [])), /unreadable_results/);
  const empty = accept(run(), { ...snapshot(1, [], "unknown"), empty: true });
  assert.equal(empty.status, "completed");
  assert.equal(empty.completeness, "visible_results_only");
  const missing = accept(run(), snapshot(1, [person(1)], "unknown"));
  assert.equal(missing.reason, "pagination_unavailable");
  assert.equal(missing.people.length, 1);
  assert.equal(missing.completeness, "partial");
});

test("page, people and elapsed-time bounds retain partial results", () => {
  const people = run(); people.limits.people = 1;
  const clipped = accept(people, snapshot(1, [person(1), person(2)], "end"));
  assert.equal(clipped.reason, "people_limit");
  assert.equal(clipped.people.length, 1);
  assert.equal(clipped.completeness, "partial");
  const pages = run(); pages.limits.pages = 1;
  assert.equal(accept(pages, snapshot()).reason, "page_limit");
  const saved = accept(run(), snapshot());
  const expired = C.acceptSnapshot(saved, snapshot(2, [person(2)]), saved.deadline);
  assert.equal(expired.reason, "time_limit");
  assert.equal(expired.people.length, 1);
});

test("CSV quotes fields and neutralizes spreadsheet formulas", () => {
  const saved = accept(run(), snapshot(1, [{ ...person(1), name: '=HYPERLINK("example")', headline: "+SUM(1,2)", location: "@cmd" }], "end"));
  const csv = C.csv(saved);
  assert.ok(csv.includes('"\'=HYPERLINK(""example"")"'));
  assert.ok(csv.includes('"\'+SUM(1,2)"'));
  assert.ok(csv.includes('"\'@cmd"'));
  assert.ok(csv.includes('"visible_results_only"'));
  assert.equal(csv.split("\r\n").length, 3);
});
