/* Local collection schema and validation. Shared by the worker and isolated page adapter. */
(function installCore(root) {
  "use strict";
  const LIMITS = Object.freeze({ pages: 20, people: 200, minutes: 15 });
  const VERSION = "linkedin-visible-connections-v1";
  function fail(code) { throw new Error(code); }
  function text(value, max = 300) {
    return typeof value === "string" ? value.replace(/\s+/g, " ").trim().slice(0, max) : "";
  }
  function linkedinURL(value) {
    if (typeof value !== "string" || value.length > 4096) fail("invalid_url");
    let url;
    try { url = new URL(value); } catch { fail("invalid_url"); }
    if (url.protocol !== "https:" || !["www.linkedin.com", "linkedin.com"].includes(url.hostname) || url.port || url.username || url.password) fail("invalid_url");
    url.hostname = "www.linkedin.com";
    url.hash = "";
    return url;
  }
  function profileURL(value) {
    const url = linkedinURL(value);
    if (!/^\/in\/[^/]+\/?$/.test(url.pathname) || /%2f|%5c/i.test(url.pathname)) fail("invalid_profile_url");
    return `https://www.linkedin.com${url.pathname.replace(/\/$/, "")}`;
  }
  function searchContext(value) {
    const url = linkedinURL(value);
    if (url.pathname !== "/search/results/people/") fail("wrong_view");
    if (url.searchParams.getAll("connectionOf").length !== 1) fail("missing_friend_filter");
    let friends;
    try { friends = JSON.parse(url.searchParams.get("connectionOf")); } catch { fail("missing_friend_filter"); }
    if (typeof friends === "string") friends = [friends];
    if (!Array.isArray(friends) || friends.length !== 1 || typeof friends[0] !== "string" || !/^[a-zA-Z0-9:_-]{1,256}$/.test(friends[0])) fail("ambiguous_friend_filter");
    const page = url.searchParams.get("page") || "1";
    if (!/^[1-9]\d{0,4}$/.test(page) || url.searchParams.getAll("page").length > 1) fail("invalid_page");
    // LinkedIn's Next action serializes a single friend as a JSON string and adds
    // these default UI flags. Pin their meaning rather than their URL encoding.
    for (const key of ["spellCorrectionEnabled", "prioritizeMessage"]) {
      if (url.searchParams.getAll(key).length > 1) fail("ambiguous_friend_filter");
    }
    const params = [...url.searchParams]
      .filter(([key, value]) => !["page", "origin", "sid", "trk"].includes(key) && !(key === "spellCorrectionEnabled" && value === "true") && !(key === "prioritizeMessage" && value === "false"))
      .map(([key, value]) => [key, key === "connectionOf" ? JSON.stringify(friends) : value]);
    params.sort(([a, av], [b, bv]) => a.localeCompare(b) || av.localeCompare(bv));
    for (const key of ["origin", "sid", "trk"]) url.searchParams.delete(key);
    return { key: JSON.stringify(params), friend_id: friends[0], page: Number(page), url: url.href };
  }
  function fingerprint(records) {
    return JSON.stringify([...new Set(records.map(row => profileURL(row.profile_url)))].sort());
  }
  function prepareSelection(selection, keywords = "") {
    if (typeof keywords !== "string" || keywords.length > 300 || /[\u0000-\u001f\u007f]/.test(keywords)) fail("invalid_keywords");
    const url = new URL(searchContext(selection.collection_url).url);
    url.searchParams.set("network", JSON.stringify(["S"]));
    url.searchParams.delete("page");
    url.searchParams.delete("keywords");
    if (keywords.trim()) url.searchParams.set("keywords", keywords.trim());
    return { ...selection, collection_url: url.href };
  }
  function collectionFilters(value) {
    const url = new URL(searchContext(value).url);
    let network = null;
    try { network = JSON.parse(url.searchParams.get("network")); } catch { /* Older saved views may omit network filters. */ }
    return { network, keywords: url.searchParams.get("keywords") || "" };
  }
  function createRun(selection, now = Date.now(), id = crypto.randomUUID()) {
    const context = searchContext(selection.collection_url);
    if (context.page !== 1) fail("start_at_first_page");
    if (!selection.actor?.key || !selection.friend?.name) fail("missing_identity");
    return {
      schema: VERSION, id, generation: id, adapter_version: "desktop-en-evidence-v3",
      started_at: new Date(now).toISOString(), updated_at: new Date(now).toISOString(),
      deadline: now + LIMITS.minutes * 60_000, limits: { ...LIMITS },
      actor: selection.actor, friend: { ...selection.friend, profile_url: profileURL(selection.friend.profile_url) },
      collection_url: context.url, context_key: context.key, tab_id: selection.tab_id,
      filters: collectionFilters(context.url),
      status: "collecting", phase: "opening", reason: "", phase_started: now,
      pages: [], people: [], skipped: { first_degree: 0, unknown_degree: 0, invalid: 0 },
      completeness: "partial",
    };
  }
  function halt(run, status, reason, now = Date.now()) {
    return { ...run, status, reason, updated_at: new Date(now).toISOString() };
  }
  function checkSnapshot(run, snapshot) {
    if (snapshot.actor?.key !== run.actor.key) fail("account_changed");
    const context = searchContext(snapshot.url);
    if (context.key !== run.context_key) fail("filters_changed");
    if (context.page !== snapshot.page) fail("page_mismatch");
    if (!Array.isArray(snapshot.records) || snapshot.records.length > 500) fail("invalid_snapshot");
    return context;
  }
  function acceptSnapshot(original, snapshot, now = Date.now()) {
    checkSnapshot(original, snapshot);
    const run = structuredClone(original);
    if (now >= run.deadline) return halt(run, "partial", "time_limit", now);
    const print = fingerprint(snapshot.records);
    const previous = run.pages.at(-1);
    if (previous?.page === snapshot.page) {
      if (previous.fingerprint !== print) fail("page_changed");
      return run; // The acknowledged page can be replayed after a worker restart.
    }
    if (snapshot.page !== (previous?.page || 0) + 1) fail("page_mismatch");
    if (run.pages.some(page => page.fingerprint === print) && print !== "[]") return halt(run, "partial", "repeated_page", now);
    if (!snapshot.records.length && !snapshot.empty) fail("unreadable_results");
    if (run.pages.length >= run.limits.pages) return halt(run, "partial", "page_limit", now);
    const seen = new Set(run.people.map(row => row.profile_url));
    const thisPage = new Set();
    let clipped = false;
    run.skipped.invalid += snapshot.invalid_count || 0;
    for (const row of snapshot.records) {
      const url = profileURL(row.profile_url);
      if (thisPage.has(url)) continue;
      thisPage.add(url);
      if (row.degree !== "2nd") {
        run.skipped[row.degree === "1st" ? "first_degree" : "unknown_degree"]++;
        continue;
      }
      if (url === run.friend.profile_url || url === run.actor.profile_url || seen.has(url)) continue;
      if (!text(row.name)) { run.skipped.invalid++; continue; }
      if (run.people.length >= run.limits.people) { clipped = true; continue; }
      seen.add(url);
      run.people.push({ profile_url: url, name: text(row.name, 200), headline: text(row.headline), location: text(row.location, 200), visible_text: text(row.visible_text, 1000), degree: "2nd", observed_at: snapshot.observed_at, source_page: snapshot.page });
    }
    run.pages.push({ page: snapshot.page, fingerprint: print, source_url: snapshot.url, observed_at: snapshot.observed_at, visible_count: snapshot.records.length });
    run.updated_at = new Date(now).toISOString();
    run.phase = "advance";
    run.phase_started = now;
    if (clipped) return halt(run, "partial", "people_limit", now);
    if (snapshot.empty || snapshot.next === "end") return { ...halt(run, "completed", "visible_results_exhausted", now), completeness: "visible_results_only" };
    if (snapshot.next !== "next") return halt(run, "partial", "pagination_unavailable", now);
    if (run.people.length >= run.limits.people) return halt(run, "partial", "people_limit", now);
    if (run.pages.length >= run.limits.pages) return halt(run, "partial", "page_limit", now);
    return run;
  }
  function result(run) {
    if (!run) fail("no_collection");
    return {
      schema: run.schema, collection_id: run.id, adapter_version: run.adapter_version,
      started_at: run.started_at, updated_at: run.updated_at, actor: run.actor, friend: run.friend,
      filters: collectionFilters(run.collection_url),
      coverage: { status: run.status, reason: run.reason, completeness: run.completeness, limits: run.limits, pages: run.pages.map(({ fingerprint: _, ...page }) => page), skipped: run.skipped },
      people: run.people,
      paths: run.people.map(person => ({ actor_key: run.actor.key, friend_profile_url: run.friend.profile_url, person_profile_url: person.profile_url, degree: person.degree, evidence: "selected_friend_connections_view", source_page: person.source_page, observed_at: person.observed_at })),
    };
  }
  function csvCell(value) {
    let valueText = String(value ?? "");
    if (/^[\s\u0000-\u001f]*[=+@-]/.test(valueText) || /^[\t\r\n]/.test(valueText)) valueText = "'" + valueText;
    return '"' + valueText.replaceAll('"', '""') + '"';
  }
  function csv(run) {
    const header = ["name", "profile_url", "headline", "location", "visible_text", "friend_name", "friend_profile_url", "degree", "observed_at", "source_page", "collection_status", "stop_reason", "coverage", "search_query"];
    const keywords = collectionFilters(run.collection_url).keywords;
    return [header, ...run.people.map(p => [p.name, p.profile_url, p.headline, p.location, p.visible_text, run.friend.name, run.friend.profile_url, p.degree, p.observed_at, p.source_page, run.status, run.reason, run.completeness, keywords])].map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
  }
  root.TinCollectorCore = Object.freeze({ LIMITS, VERSION, text, linkedinURL, profileURL, searchContext, fingerprint, prepareSelection, createRun, halt, checkSnapshot, acceptSnapshot, result, csv });
})(globalThis);
