import assert from "node:assert/strict";
import test, { before, after } from "node:test";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import "../../src/collection/core.js";
import { selection, snapshot, person, card, profileHTML, resultsHTML, searchURL, escape } from "./fixtures.mjs";

let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });
const C = globalThis.TinCollectorCore;
async function pageFor(t, html, url = searchURL()) {
  const context = await browser.newContext();
  t.after(() => context.close());
  await context.route("**/*", route => route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: html }));
  const page = await context.newPage();
  await page.goto(url);
  await page.addScriptTag({ path: fileURLToPath(new URL("../../src/collection/core.js", import.meta.url)) });
  await page.addScriptTag({ path: fileURLToPath(new URL("../../src/collection/page-evidence.js", import.meta.url)) });
  await page.addScriptTag({ path: fileURLToPath(new URL("../../src/collection/linkedin.js", import.meta.url)) });
  await page.evaluate(() => { globalThis.chrome = { runtime: { sendMessage: async () => ({ ok: true }) } }; });
  return page;
}
const read = (page, run, timeout = 1000) => page.evaluate(({ run, timeout }) => TinLinkedIn.snapshot(run, { poll: 15, timeout }), { run, timeout });

test("reads only the bounded embedded search identifier and rejects ambiguity", async t => {
  const query = "voyagerSearchDashClusters." + "a".repeat(32);
  const html = resultsHTML() + `<code hidden id="bpr-guid-fixture" data-request="/voyager/api/graphql?queryId=${query}">{"private":"not returned"}</code><main><p>voyagerSearchDashClusters.${"b".repeat(32)}</p></main>`;
  const page = await pageFor(t, html);
  const evidence = await page.evaluate(() => TinLinkedIn.searchContract());
  assert.deepEqual(evidence.actor,selection.actor);assert.equal(evidence.query_id,query);
  assert.equal(evidence.diagnostics.query_found,true);
  assert.equal(JSON.stringify(evidence).includes("not returned"),false);
  await page.evaluate(() => {
    const data = document.createElement("script");data.type="application/json";data.textContent=JSON.stringify({queryId:"voyagerSearchDashClusters."+"b".repeat(32)});document.body.append(data);
  });
  await assert.rejects(page.evaluate(() => TinLinkedIn.searchContract()), /ambiguous_search_contract/);
});

test("reads cached search requests from resource timing but ignores other origins", async t => {
  const page = await pageFor(t, resultsHTML());
  await page.evaluate(() => {
    Object.defineProperty(performance,"getEntriesByType",{value:()=>[
      {name:"https://unrelated.invalid/voyager/api/graphql?queryId=voyagerSearchDashClusters."+"b".repeat(32)},
      {name:"https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashClusters."+"a".repeat(32)}
    ]});
  });
  assert.equal((await page.evaluate(() => TinLinkedIn.searchContract())).query_id,"voyagerSearchDashClusters."+"a".repeat(32));
});

const semanticNavigation = `<div role="banner"><a href="${selection.actor.profile_url}">${selection.actor.name}</a></div>`;
for (const variant of ["h2", "role-heading", "plain-text", "aria-hidden", "hidden-first-heading", "no-landmark"]) {
  test(`selects the same profile with ${variant} markup and no site-specific classes`, async t => {
    const title = variant === "h2" ? `<h2>${selection.friend.name}</h2>` : variant === "role-heading" ? `<div role="heading" aria-level="2">${selection.friend.name}</div>` : variant === "aria-hidden" ? `<h1 aria-hidden="true">${selection.friend.name}</h1>` : `<div><span>${selection.friend.name}</span></div>`;
    const content = `<article><div>${title}<span>· 1st</span></div><p>Synthetic profile description</p><a href="${escape(searchURL())}">500+ connections</a></article>`;
    const html = `<!doctype html><html lang="en"><body>${semanticNavigation}${variant === "hidden-first-heading" ? '<h1 hidden>Stale profile</h1>' : ""}<div ${variant === "no-landmark" ? "" : 'role="main"'}>${content}</div><aside><div>Another Member</div><span>1st</span><a href="${escape(searchURL(1, "another-member"))}">500+ connections</a></aside></body></html>`;
    const page = await pageFor(t, html, selection.friend.profile_url);
    const actual = await page.evaluate(() => TinLinkedIn.profile());
    assert.equal(actual.friend.name, selection.friend.name);
    assert.equal(actual.collection_url, selection.collection_url);
    assert.equal(actual.actor.key, selection.actor.key);
  });
}

test("does not borrow a relationship badge from another profile card", async t => {
  const html = `<!doctype html><html lang="en"><body>${semanticNavigation}<main><article><div>${selection.friend.name}</div><a href="${escape(searchURL())}">500+ connections</a></article><article><div>Other Person</div><span>1st</span></article></main></body></html>`;
  const page = await pageFor(t, html, selection.friend.profile_url);
  await assert.rejects(page.evaluate(() => TinLinkedIn.profile()), /unsupported_profile/);
});

test("layout diagnostics retain structural evidence without profile data or page content", async t => {
  const html = profileHTML().replace("<section>", '<section data-secret="private-session-value">');
  const page = await pageFor(t, html, selection.friend.profile_url);
  const report = await page.evaluate(() => TinLinkedIn.diagnostics());
  assert.equal(report.selection.ok, true);
  assert.equal(report.account.ok, true);
  assert.equal(report.page_kind, "profile");
  assert.equal(report.connection_links.length, 1);
  const serialized = JSON.stringify(report);
  for (const secret of [selection.friend.name, selection.actor.name, selection.friend.profile_url, selection.actor.profile_url, "test-friend-id", "private-session-value", "500+"]) assert.ok(!serialized.includes(secret), secret);
  await page.locator("h1").evaluate(node => node.remove());
  const failed = await page.evaluate(() => TinLinkedIn.diagnostics());
  assert.equal(failed.selection.ok, false);
  assert.equal(failed.headings.length, 0);
});

test("rejects multiple possible friend filters within the selected profile card", async t => {
  const html = `<!doctype html><html lang="en"><body>${semanticNavigation}<main><article><div>${selection.friend.name}</div><span>1st</span><a href="${escape(searchURL())}">Connections</a><a href="${escape(searchURL(1, "another-member"))}">Connections</a></article></main></body></html>`;
  const page = await pageFor(t, html, selection.friend.profile_url);
  await assert.rejects(page.evaluate(() => TinLinkedIn.profile()), /ambiguous_friend_filter/);
});

test("distinguishes all connections from the separate mutual-connections link", async t => {
  const all = searchURL() + '&network=%5B%22F%22%2C%22S%22%5D';
  const mutual = searchURL() + '&network=%5B%22F%22%5D';
  const html = `<!doctype html><html lang="en"><body>${semanticNavigation}<main><article><div><a href="${selection.friend.profile_url}"><h2>${selection.friend.name}</h2></a><span>· 1st</span></div><div><a href="${escape(all)}">500+ connections</a></div><a href="${escape(mutual)}">Test Person and 12 other mutual connections</a></article></main></body></html>`;
  const page = await pageFor(t, html, selection.friend.profile_url);
  const actual = await page.evaluate(() => TinLinkedIn.profile());
  assert.equal(C.searchContext(actual.collection_url).key, C.searchContext(all).key);
  assert.equal(actual.friend.name, selection.friend.name);
});

test("extracts classless result cards and follows accessible pagination without reading the sidebar", async t => {
  const row = (id, degree) => `<div><div><a href="${person(id).profile_url}"><span>${person(id).name}</span></a><span>${degree}</span></div><p>Synthetic company</p><button>Connect</button></div>`;
  const body = (page, next) => `<div role="main"><div role="list">${row(page, "2nd")}${row(10 + page, "1st")}</div><nav aria-label="Results pages"><button aria-current="page" aria-label="Page ${page}"></button><div role="button" aria-label="Next page" ${next ? "" : 'aria-disabled="true"'} tabindex="0"></div></nav><aside>${row(99, "2nd")}</aside></div>`;
  const page = await pageFor(t, `<!doctype html><html lang="en"><body>${semanticNavigation}${body(1, true)}</body></html>`);
  let run = C.createRun(selection);
  run = C.acceptSnapshot(run, await read(page, run));
  assert.deepEqual(run.people.map(p => p.profile_url), [person(1).profile_url]);
  await page.evaluate(({ html, url }) => {
    document.querySelector('[aria-label="Next page"]').onclick = () => {
      history.pushState({}, "", url);
      document.querySelector('[role="main"]').outerHTML = html;
    };
  }, { html: body(2, false), url: searchURL(2) });
  await page.evaluate(run => TinLinkedIn.advance(run), run);
  run = C.acceptSnapshot(run, await read(page, run));
  assert.equal(run.status, "completed");
  assert.deepEqual(run.people.map(p => p.profile_url), [person(1).profile_url, person(2).profile_url]);
});

test("selects a visible first-degree connection and records its observed account and filter", async t => {
  const page = await pageFor(t, profileHTML(), selection.friend.profile_url);
  const actual = await page.evaluate(() => TinLinkedIn.profile());
  assert.equal(actual.friend.profile_url, selection.friend.profile_url);
  assert.equal(actual.collection_url, selection.collection_url);
  assert.equal(actual.actor.key, selection.actor.key);
  await page.locator(".dist-value").evaluate(el => { el.textContent = "2nd"; });
  await assert.rejects(page.evaluate(() => TinLinkedIn.profile()), /friend_not_first_degree/);
  await page.locator(".dist-value").evaluate(el => { el.textContent = "1st"; });
  await page.locator("section a").evaluate(el => { el.hidden = true; });
  await assert.rejects(page.evaluate(() => TinLinkedIn.profile()), /connections_unavailable/);
});

test("reads two paginated pages, excludes recommendations and deduplicates overlapping rows", async t => {
  const page = await pageFor(t, resultsHTML(1, [person(1), person(2, "1st")]));
  await page.evaluate(html => document.querySelector("main").insertAdjacentHTML("beforeend", `<aside>${html}</aside>`), card(person(99)));
  let run = C.createRun(selection);
  run = C.acceptSnapshot(run, await read(page, run));
  assert.equal(run.people.length, 1);
  await page.evaluate(({ url, html }) => {
    document.querySelector('[aria-label="Next"]').onclick = () => {
      history.pushState({}, "", url);
      const parsed = new DOMParser().parseFromString(html, "text/html");
      document.querySelector("main").replaceWith(parsed.querySelector("main"));
    };
  }, { url: searchURL(2), html: resultsHTML(2, [person(1), person(3)], "end") });
  await page.evaluate(run => TinLinkedIn.advance(run), run);
  run = C.acceptSnapshot(run, await read(page, run));
  assert.equal(run.status, "completed");
  assert.deepEqual(run.people.map(p => p.name), [person(1).name, person(3).name]);
  assert.equal(run.pages.length, 2);
});

test("waits for both the URL and rendered page number to agree", async t => {
  const page = await pageFor(t, resultsHTML(1, [person(1)]), searchURL(2));
  await page.evaluate(html => { setTimeout(() => { document.body.innerHTML = new DOMParser().parseFromString(html, "text/html").body.innerHTML; }, 120); }, resultsHTML(2, [person(2)], "end"));
  const result = await read(page, C.createRun(selection));
  assert.equal(result.page, 2);
  assert.equal(result.records[0].profile_url, person(2).profile_url);
});

test("collects lazy virtualized rows and can reread them from the top", async t => {
  const page = await pageFor(t, resultsHTML(1, [person(1)]));
  await page.evaluate(({ first, second }) => {
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (options) {
      original.call(this, options);
      if (this.matches("main") && options.block === "start") this.querySelector("ul").innerHTML = first;
      else if (this.matches("li") && this.textContent.includes("Test Person 1")) document.querySelector("ul").innerHTML = second;
    };
  }, { first: card(person(1)), second: card(person(2)) });
  const run = C.createRun(selection);
  const first = await read(page, run);
  const second = await read(page, run);
  assert.equal(first.records.length, 2);
  assert.deepEqual(second.records, first.records);
});

test("account changes and altered friend filters are rejected", async t => {
  const page = await pageFor(t, resultsHTML());
  const run = C.createRun(selection);
  await page.locator(".global-nav__me-profile-link").evaluate(el => { el.href = "https://www.linkedin.com/in/different-owner"; });
  await assert.rejects(read(page, run), /account_changed/);
  await page.locator(".global-nav__me-profile-link").evaluate((el, url) => { el.href = url; }, selection.actor.key);
  await page.evaluate(url => history.pushState({}, "", url), searchURL(1, "different-friend"));
  await assert.rejects(read(page, run), /filters_changed/);
});

test("stable avatar identity ignores expiring image parameters and rejects placeholders", async t => {
  const page = await pageFor(t, profileHTML(), selection.friend.profile_url);
  await page.evaluate(() => {
    document.querySelector("header").innerHTML = '<img class="global-nav__me-photo" alt="Test Owner" src="https://media.licdn.com/dms/image/v2/TEST/profile-displayphoto-shrink_100_100/0/1?e=1">';
  });
  const a = await page.evaluate(() => TinLinkedIn.profile());
  await page.locator("img").evaluate(el => { el.src = "https://media.licdn.com/dms/image/v2/TEST/profile-displayphoto-shrink_400_400/0/1?e=2"; });
  const b = await page.evaluate(() => TinLinkedIn.profile());
  assert.equal(a.actor.key, b.actor.key);
  assert.equal(a.actor.source, "browser_avatar_marker");
  await page.locator("img").evaluate(el => { el.src = "https://www.linkedin.com/ghost-person.svg"; });
  await assert.rejects(page.evaluate(() => TinLinkedIn.profile()), /account_unavailable/);
});

test("checkpoints, platform limits and unsupported languages stop the read", async t => {
  const page = await pageFor(t, resultsHTML());
  const run = C.createRun(selection);
  await page.evaluate(() => document.documentElement.lang = "de");
  await assert.rejects(read(page, run), /unsupported_language/);
  await page.evaluate(() => { document.documentElement.lang = "en"; document.querySelector("main").insertAdjacentHTML("afterbegin", '<h2>Commercial use limit</h2>'); });
  await assert.rejects(read(page, run), /platform_limit/);
  await page.evaluate(() => history.pushState({}, "", "/checkpoint/challenge"));
  await assert.rejects(read(page, run), /login_or_checkpoint/);
});

test("missing pagination is partial and missing page identity is never advanced", async t => {
  const page = await pageFor(t, resultsHTML(1, [person(1)], "unknown"));
  const run = C.createRun(selection);
  assert.equal(C.acceptSnapshot(run, await read(page, run)).reason, "pagination_unavailable");
  await page.evaluate(() => document.querySelector("main").insertAdjacentHTML("beforeend", '<div class="artdeco-pagination"><button aria-label="Next">Next</button></div>'));
  await assert.rejects(read(page, run), /unsupported_pagination/);
});

test("an unreadable result area cannot be mistaken for an empty final page", async t => {
  const page = await pageFor(t, resultsHTML(1, [], "end"));
  const run = C.createRun(selection);
  assert.equal(C.acceptSnapshot(run, await read(page, run)).status, "completed");
  await page.locator("h2").evaluate(el => el.remove());
  await assert.rejects(read(page, run, 120), /unstable_results/);
});

const hiddenNextPager = `<div><button>Previous</button><div><button aria-label="Page 1" aria-current="false">1</button><button aria-label="Page 2" aria-current="true">2</button></div><button style="visibility:hidden"><span>Next</span></button></div>`;
const hiddenNextHTML = () => resultsHTML(2, [person(2)], "unknown").replace("</main>", `${hiddenNextPager}</main>`);

test("a hidden Next control and a numbered pager ending at the current page complete the collection", async t => {
  const page = await pageFor(t, hiddenNextHTML(), searchURL(2));
  const run = C.acceptSnapshot(C.createRun(selection), snapshot());
  const result = C.acceptSnapshot(run, await read(page, run));
  assert.equal(result.status, "completed");
  assert.equal(result.reason, "visible_results_exhausted");
  assert.equal(result.completeness, "visible_results_only");
  assert.equal(result.people.length, 2);
  assert.equal(result.pages.length, 2);
  await assert.rejects(page.evaluate(run => TinLinkedIn.advance(run), result), /navigation_changed/);
});

test("hidden Next cannot imply completion with missing, ambiguous or unfinished pagination", async t => {
  const page = await pageFor(t, hiddenNextHTML(), searchURL(2));
  const run = C.acceptSnapshot(C.createRun(selection), snapshot());
  const cases = [
    hiddenNextPager.replace('style="visibility:hidden"', 'hidden'),
    hiddenNextPager.replace('<button>Previous</button>', ''),
    hiddenNextPager.replace('<button>Previous</button>', '<button disabled>Previous</button>'),
    hiddenNextPager.replace('aria-current="true"', 'aria-current="false"'),
    hiddenNextPager.replace('aria-current="false"', 'aria-current="true"'),
    hiddenNextPager.replace('</div><button style', '<button aria-label="Page 3">3</button></div><button style'),
    hiddenNextPager.replace('</div><button style', '<span>…</span></div><button style'),
    hiddenNextPager.replace('</div><button style', '<button aria-label="Load more pages">More</button></div><button style'),
    hiddenNextPager.replace('Page 1', 'Page 2'),
    hiddenNextPager.replace('<button style="visibility:hidden"><span>Next</span></button>', ''),
    `<aside>${hiddenNextPager}</aside>`,
    `<div hidden>${hiddenNextPager}</div>`,
  ];
  for (const pager of cases) {
    await page.locator("main").evaluate((main, html) => { main.innerHTML = html; }, `<ul>${card(person(2))}</ul>${pager}`);
    const result = C.acceptSnapshot(run, await read(page, run));
    assert.equal(result.reason, "pagination_unavailable", pager);
    assert.equal(result.people.length, 2);
  }
});

test("hidden Next waits for the numbered pager to agree with the URL", async t => {
  const html = hiddenNextHTML().replace('Page 2', 'Page 3').replace('>2</button>', '>3</button>');
  const page = await pageFor(t, html, searchURL(2));
  const run = C.acceptSnapshot(C.createRun(selection), snapshot());
  await page.locator('[aria-current="true"]').evaluate(node => setTimeout(() => { node.setAttribute("aria-label", "Page 2"); node.textContent = "2"; }, 120));
  assert.equal((await read(page, run)).next, "end");
});

test("rechecks cancellation immediately before Next and refuses a changed Next link", async t => {
  const page = await pageFor(t, resultsHTML());
  const run = C.acceptSnapshot(C.createRun(selection), snapshot());
  await page.evaluate(() => {
    globalThis.clickCount = 0;
    document.querySelector('[aria-label="Next"]').onclick = () => { globalThis.clickCount++; };
    chrome.runtime.sendMessage = async () => ({ ok: false });
  });
  await assert.rejects(page.evaluate(run => TinLinkedIn.advance(run), run), /collection_cancelled/);
  assert.equal(await page.evaluate(() => clickCount), 0);
  await page.evaluate(url => {
    const link = document.createElement("a"); link.href = url; link.textContent = "Next";
    document.querySelector('[aria-label="Next"]').replaceWith(link);
    chrome.runtime.sendMessage = async () => ({ ok: true });
  }, searchURL(2, "other-friend"));
  await assert.rejects(page.evaluate(run => TinLinkedIn.advance(run), run), /navigation_changed/);
  await page.evaluate(generation => { globalThis.TinCollectorCancelled = new Set([generation]); }, run.generation);
  await assert.rejects(read(page, run), /collection_cancelled/);
});
