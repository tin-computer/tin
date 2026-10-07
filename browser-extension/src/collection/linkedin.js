/* Packaged, isolated-world adapter. No cookies, private API calls or page-world execution. */
(function installLinkedIn(root) {
  "use strict";
  const C = root.TinCollectorCore;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const fail = code => { throw new Error(code); };
  const E = root.TinPageEvidence;
  const txt = E.text;
  const visible = E.visible;
  function guard() {
    if (location.origin !== "https://www.linkedin.com") fail("wrong_origin");
    if (/^\/(checkpoint|challenge|login|uas|authwall)(\/|$)/.test(location.pathname) || document.querySelector('input[name="session_password"], #captcha-internal, iframe[src*="captcha"], form[action*="checkpoint"]')) fail("login_or_checkpoint");
    const lang = document.documentElement.lang;
    if (lang && !/^en(?:-|$)/i.test(lang)) fail("unsupported_language");
    const notices = [...document.querySelectorAll('[role="alert"], .artdeco-inline-feedback, main h1, main h2')].filter(visible).map(txt).join(" ");
    if (/commercial use limit|search limit|unusual activity|temporarily restricted|security verification|too many requests/i.test(notices)) fail("platform_limit");
  }
  function profile() {
    guard();
    return { actor: E.account(), ...E.selectedProfile() };
  }
  function searchContract() {
    guard();
    if (location.pathname !== "/search/results/people/") fail("wrong_page");
    const ids = new Set();
    const resources = performance.getEntriesByType("resource").slice(-500);
    const nodes = [...document.querySelectorAll('code[id^="bpr-guid-"], code[data-request], script[type="application/json"], [data-request-url]')].slice(0, 100);
    const accept = value => {
      if (/^voyagerSearchDashClusters\.[a-f0-9]{20,64}$/.test(value || "")) ids.add(value);
    };
    const request = value => {
      try {
        const url = new URL(value, location.origin);
        if (url.origin === location.origin && url.pathname === "/voyager/api/graphql") accept(url.searchParams.get("queryId"));
      } catch { /* Not a request URL. */ }
    };
    // Initial results can be embedded in the document or loaded from cache without
    // a new request-header event. Read the query identifier, never the result data.
    for (const entry of resources) request(entry.name);
    let bytes = 0;
    for (const node of nodes) {
      request(node.getAttribute("data-request") || node.getAttribute("data-request-url") || "");
      const content = node.textContent || "";
      bytes += content.length;
      if (bytes > 2_000_000) break;
      for (const match of content.matchAll(/\bvoyagerSearchDashClusters\.[a-f0-9]{20,64}\b/g)) accept(match[0]);
    }
    if (ids.size > 1) fail("ambiguous_search_contract");
    return { actor: E.account(), query_id: [...ids][0] || null,
      diagnostics: {resource_entries:resources.length,bootstrap_nodes:nodes.length,query_found:ids.size === 1} };
  }
  function readPage(run) {
    if (root.TinCollectorCancelled?.has(run.generation)) fail("collection_cancelled");
    guard();
    const currentActor = E.account();
    const context = C.searchContext(location.href);
    if (context.key !== run.context_key) fail("filters_changed");
    if (currentActor.key !== run.actor.key) fail("account_changed");
    const container = E.content();
    if (!container || !visible(container)) fail("page_loading");
    if ([...container.querySelectorAll('[aria-busy="true"], [role="progressbar"]')].some(visible)) fail("page_loading");
    const cards = E.resultCards(container);
    const records = cards.map(card => card.record);
    const pagination = E.pagination(container, context.page);
    const empty = !records.length && [...container.querySelectorAll('h1, h2, h3, p, [role="status"], [role="heading"]')].some(node => visible(node) && !E.excluded(node) && /^(no results found|no results for|no matching results)(?:[.!]|$|\s)/i.test(txt(node)));
    return {
      actor: currentActor, url: context.url, page: context.page, records, invalid_count: 0, empty,
      ...pagination, observed_at: new Date().toISOString(), lastCard: cards.at(-1)?.node,
    };
  }
  async function snapshot(run, options = {}) {
    const timeout = options.timeout ?? 12_000;
    const poll = options.poll ?? 350;
    const started = Date.now();
    // Revisit the top before each read so virtualized rows can be observed again.
    E.content()?.scrollIntoView({ block: "start", behavior: "instant" });
    const rows = new Map();
    let stable = 0, lastSignature = "", firstPage = null, last;
    while (Date.now() - started < timeout) {
      let page;
      try { page = readPage(run); } catch (error) {
        if (error.message !== "page_loading") throw error;
        await sleep(poll); continue;
      }
      if (firstPage !== null && firstPage !== page.page) fail("page_changed_during_read");
      firstPage = page.page;
      for (const row of page.records) rows.set(row.profile_url, row);
      if (rows.size > 500) fail("page_too_large");
      // Scroll the last rendered card into view to expose lazy / virtualized rows.
      page.lastCard?.scrollIntoView({ block: "end", behavior: "instant" });
      const signature = JSON.stringify([page.page, [...rows.values()], page.next, page.empty, page.invalid_count]);
      stable = signature === lastSignature ? stable + 1 : 0;
      lastSignature = signature;
      last = page;
      if (stable >= 3 && (rows.size || page.empty)) {
        const { lastCard: _, nextButton: __, ...data } = last;
        return { ...data, records: [...rows.values()] };
      }
      await sleep(poll);
    }
    fail(last ? "unstable_results" : "page_loading");
  }
  async function advance(run) {
    const snapshotNow = await snapshot(run);
    const previous = run.pages.at(-1);
    if (snapshotNow.page !== previous?.page || C.fingerprint(snapshotNow.records) !== previous.fingerprint) fail("page_changed");
    const page = readPage(run);
    if (page.next !== "next" || page.page !== previous.page) fail("navigation_changed");
    if (page.nextButton.tagName === "A") {
      const target = C.searchContext(page.nextButton.href);
      if (target.key !== run.context_key || target.page !== previous.page + 1) fail("navigation_changed");
    }
    const allowed = await chrome.runtime.sendMessage({ namespace: "tin.linkedin.collector.v1", type: "CAN_ADVANCE", generation: run.generation, collection_id: run.id });
    if (!allowed?.ok || root.TinCollectorCancelled?.has(run.generation)) fail("collection_cancelled");
    page.nextButton.click();
    return { clicked: true };
  }
  root.TinLinkedIn = Object.freeze({ profile, snapshot, advance, searchContract, diagnostics: E.diagnostics });
})(globalThis);
