/* Read rendered evidence without depending on LinkedIn's generated class names. */
(function installEvidence(root) {
  "use strict";
  const C = root.TinCollectorCore;
  const fail = code => { throw new Error(code); };
  const text = node => C.text(node?.innerText || node?.textContent || "", 2000);
  function visible(node) {
    if (!node || node.closest("[hidden], script, style, template") || !node.getClientRects().length) return false;
    const style = getComputedStyle(node);
    // aria-hidden describes accessibility, not whether a person can see the element.
    return !["hidden", "collapse"].includes(style.visibility) && style.opacity !== "0" &&
      !/rect\(0px,? 0px,? 0px,? 0px\)/.test(style.clip) && style.clipPath !== "inset(50%)";
  }
  const label = node => C.text(node?.getAttribute("aria-label") || text(node));
  const excluded = node => !!node.closest('aside, [role="complementary"], nav, [role="navigation"], footer, [role="dialog"]');
  function content() {
    const candidates = [...document.querySelectorAll('main, [role="main"]')].filter(visible);
    const outer = candidates.filter(node => !candidates.some(other => other !== node && other.contains(node)));
    if (outer.length > 1) fail("ambiguous_content");
    return outer[0] || document.body;
  }
  function profileURL(node) {
    try { return C.profileURL(node.href); } catch { return null; }
  }
  function profileLinks(scope) {
    return [...scope.querySelectorAll('a[href*="/in/"]')].filter(node => visible(node) && !excluded(node) && profileURL(node));
  }
  function leaves(scope) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const found = [];
    let node, visited = 0;
    while ((node = walker.nextNode())) {
      if (++visited > 1500) fail("page_too_large");
      const value = C.text(node.nodeValue);
      if (value && visible(node.parentElement) && !excluded(node.parentElement)) found.push({ node, value });
    }
    return found;
  }
  function degree(value) {
    return value.match(/^(?:[·•]\s*)?([123](?:st|nd|rd))\+?(?:\s+(?:degree(?: connection)?|connection))?$/i)?.[1]?.toLowerCase() || null;
  }
  function relations(scope) {
    return leaves(scope).flatMap(item => {
      const exact = degree(item.value);
      if (exact) return [{ ...item, degree: exact, prefix: "" }];
      const inline = item.value.match(/^(.{2,160}?)\s*[·•]\s*([123](?:st|nd|rd))\+?$/);
      return inline ? [{ ...item, degree: inline[2], prefix: inline[1] }] : [];
    });
  }
  function nameBefore(scope, relation, targetURL) {
    if (relation.prefix) return relation.prefix;
    const links = profileLinks(scope).filter(link => profileURL(link) === targetURL && text(link));
    const names = [...new Set(links.map(text))];
    if (names.length === 1) return names[0];
    const items = leaves(scope);
    const index = items.findIndex(item => item.node === relation.node);
    for (let i = index - 1; i >= Math.max(0, index - 4); i--) {
      const item = items[i];
      if (/^[·•|\s]+$/.test(item.value) || /^(verified|premium|verification badge)$/i.test(item.value)) continue;
      const enclosingLink = item.node.parentElement.closest("a[href]");
      if (enclosingLink && profileURL(enclosingLink) && profileURL(enclosingLink) !== targetURL) return null;
      return item.value.length <= 160 && /\p{L}/u.test(item.value) ? item.value : null;
    }
    return null;
  }
  function selectedProfile() {
    const targetURL = C.profileURL(location.href);
    const boundary = content();
    const links = [...boundary.querySelectorAll('a[href*="/search/results/people/"]')].filter(node => visible(node) && !excluded(node) && /connections/i.test(label(node)) && !/mutual|shared/i.test(label(node)));
    const selections = [];
    let sawDegree = false;
    for (const link of links) {
      let context;
      try { context = C.searchContext(link.href); } catch { continue; }
      if (context.page !== 1) continue;
      // The smallest shared card must contain the relationship and connection link.
      // Never borrow a badge or a name from a sidebar or from another profile card.
      for (let scope = link.parentElement; scope && scope !== boundary && scope !== document.body; scope = scope.parentElement) {
        const badges = relations(scope);
        if (!badges.length) continue;
        sawDegree = true;
        if (badges.length !== 1) break;
        const name = nameBefore(scope, badges[0], targetURL);
        if (!name) break;
        if (badges[0].degree !== "1st") fail("friend_not_first_degree");
        selections.push({ name, context });
        break;
      }
    }
    if (!selections.length) fail(links.length && !sawDegree ? "unsupported_profile" : "connections_unavailable");
    if (new Set(selections.map(item => JSON.stringify([item.name, item.context.key]))).size !== 1) fail("ambiguous_friend_filter");
    return { friend: { name: C.text(selections[0].name, 200), profile_url: targetURL, degree: "1st", observed_at: new Date().toISOString() }, collection_url: selections[0].context.url };
  }
  function account() {
    const scopes = [...document.querySelectorAll('header, [role="banner"], nav, [role="navigation"]')].filter(node => visible(node) && !node.closest('main, [role="main"], aside'));
    const links = scopes.flatMap(scope => [...scope.querySelectorAll('a[href*="/in/"]')]).filter(node => visible(node) && profileURL(node));
    const urls = [...new Set(links.map(profileURL))];
    if (urls.length === 1) return { key: urls[0], profile_url: urls[0], name: C.text(text(links[0]), 200), source: "browser_profile_link" };
    const photos = scopes.flatMap(scope => [...scope.querySelectorAll("img")]).filter(visible);
    const identities = photos.flatMap(photo => {
      try {
        const url = new URL(photo.currentSrc || photo.src);
        if (url.protocol !== "https:" || url.hostname !== "media.licdn.com" || !url.pathname.includes("/profile-displayphoto") || !url.pathname.includes("/dms/image/")) return [];
        return [{ key: `avatar:${url.pathname.split("/profile-displayphoto")[0]}`, name: C.text(photo.alt, 200), source: "browser_avatar_marker" }];
      } catch { return []; }
    });
    if (new Set(identities.map(item => item.key)).size === 1) return identities[0];
    fail("account_unavailable");
  }
  function resultCards(boundary) {
    const found = new Map();
    for (const link of profileLinks(boundary)) {
      const url = profileURL(link);
      if (!text(link)) continue; // A photo-only duplicate cannot supply the person's name.
      let evidence;
      for (let scope = link.parentElement; scope && scope !== boundary && scope !== document.body; scope = scope.parentElement) {
        const urls = new Set(profileLinks(scope).map(profileURL));
        if (urls.size !== 1) break;
        const badges = relations(scope);
        if (badges.length > 1) break;
        if (!badges.length) continue;
        if ([...scope.querySelectorAll('button, a, [role="button"]')].some(node => /^(next|next page)$/i.test(label(node)))) break;
        const prior = found.get(url);
        const record = { profile_url: url, name: C.text(text(link), 200), degree: badges[0].degree,
          headline: text(scope.querySelector('[itemprop="jobTitle"]')), location: text(scope.querySelector('[itemprop="addressLocality"]')), visible_text: text(scope) };
        if (prior && prior.record.degree !== record.degree) fail("ambiguous_results");
        evidence = { node: scope, record };
        if (scope.matches('li, article, [role="listitem"]')) break;
      }
      if (evidence) found.set(url, evidence);
    }
    if (found.size > 500) fail("page_too_large");
    return [...found.values()];
  }
  function hiddenNextEndsResults(next, boundary, page) {
    // Some layouts deliberately hide Next on the final page instead of disabling it.
    // Require the numbered pager as corroboration; a missing/hidden control alone
    // could be an incomplete render and must never imply a complete collection.
    if (getComputedStyle(next).visibility !== "hidden" || !visible(next.parentElement)) return false;
    const pageNumber = node => Number(label(node).match(/^(?:page\s+)?([1-9]\d*)$/i)?.[1]) || null;
    for (let scope = next.parentElement; scope && scope !== boundary && scope !== document.body; scope = scope.parentElement) {
      if (scope.querySelector('a[href*="/in/"]')) return false;
      const controls = [...scope.querySelectorAll('button, a, [role="button"]')].filter(visible);
      const current = controls.filter(node => ["true", "page"].includes(node.getAttribute("aria-current")));
      if (!current.length) continue;
      if (current.length !== 1 || !pageNumber(current[0])) return false;
      if (pageNumber(current[0]) !== page) fail("page_loading");
      const previous = controls.filter(node => /^(previous|previous page)$/i.test(label(node)));
      if (previous.length !== 1 || previous[0].disabled || previous[0].getAttribute("aria-disabled") === "true") return false;
      if (controls.some(node => node !== previous[0] && !pageNumber(node)) || /…|\.{3}/.test(text(scope))) return false;
      const numbers = controls.filter(node => node !== previous[0]).map(pageNumber);
      return numbers.length >= 2 && numbers.at(-1) === page && numbers.every((number, index) => !index || number === numbers[index - 1] + 1);
    }
    return false;
  }
  function pagination(boundary, page) {
    const candidates = [...boundary.querySelectorAll('button, a, [role="button"]')].filter(node => !node.closest('aside, [role="complementary"], [role="dialog"]') && /^(next|next page)$/i.test(label(node)));
    const buttons = candidates.filter(visible);
    if (buttons.length > 1) fail("unsupported_pagination");
    if (!buttons.length) return { next: candidates.length === 1 && hiddenNextEndsResults(candidates[0], boundary, page) ? "end" : "unknown", nextButton: null };
    const next = buttons[0];
    let observed = null;
    for (let scope = next.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
      const current = [...scope.querySelectorAll('[aria-current="true"], [aria-current="page"]')].filter(visible);
      const numbers = [...new Set(current.map(node => label(node).match(/\d+/)?.[0]).filter(Boolean))];
      if (numbers.length > 1) fail("unsupported_pagination");
      if (numbers.length === 1) { observed = Number(numbers[0]); break; }
      if (scope === boundary) break;
    }
    if (observed === null) fail("unsupported_pagination");
    if (observed !== page) fail("page_loading");
    return { next: next.disabled || next.getAttribute("aria-disabled") === "true" ? "end" : "next", nextButton: next };
  }
  function diagnostics() {
    // Counts and structural facts only: no names, page URLs, profile IDs or page HTML.
    const check = operation => {
      try { return { ok: true, ...operation() }; }
      catch (error) { return { ok: false, code: /^[a-z_]{1,64}$/.test(error.message) ? error.message : "unexpected_error" }; }
    };
    const safeRole = node => ["main", "heading", "banner", "navigation", "complementary", "list", "listitem", "button", "link"].includes(node.getAttribute("role")) ? node.getAttribute("role") : null;
    const connections = [...document.querySelectorAll('a[href*="/search/results/people/"]')].filter(node => visible(node) && /connections/i.test(label(node))).slice(0, 10);
    return {
      schema: "tin-linkedin-layout-diagnostics-v1",
      page_kind: /^\/in\/[^/]+\/?$/.test(location.pathname) ? "profile" : location.pathname === "/search/results/people/" ? "people_search" : "other",
      main_elements: document.querySelectorAll('main, [role="main"]').length,
      frames: document.querySelectorAll("iframe").length,
      navigation: [...document.querySelectorAll('header, [role="banner"], nav, [role="navigation"]')].slice(0, 20).map(node => ({
        tag: node.tagName, role: safeRole(node), rendered: visible(node),
        in_main: !!node.closest('main, [role="main"], aside'),
        profile_links: [...node.querySelectorAll('a[href*="/in/"]')].filter(visible).length,
        images: [...node.querySelectorAll('img')].filter(visible).length,
      })),
      avatar_images: [...document.querySelectorAll('img')].filter(node => /\/profile-displayphoto/.test(node.currentSrc || node.src)).slice(0, 20).map(node => ({
        rendered: visible(node), in_main: !!node.closest('main, [role="main"], aside'),
        ancestors: (() => { const nodes = []; for (let parent = node.parentElement; parent && parent !== document.body && nodes.length < 6; parent = parent.parentElement) nodes.push({tag:parent.tagName,role:safeRole(parent)}); return nodes; })(),
      })),
      headings: [...document.querySelectorAll('h1, h2, [role="heading"]')].slice(0, 20).map(node => ({ tag: node.tagName, role: safeRole(node), rendered: visible(node), aria_hidden: !!node.closest('[aria-hidden="true"]'), in_main: !!node.closest('main, [role="main"]') })),
      account: check(() => ({ source: account().source })),
      selection: check(() => { selectedProfile(); return {}; }),
      connection_links: connections.map(link => {
        const ancestors = [];
        for (let scope = link.parentElement; scope && scope !== document.body && ancestors.length < 6; scope = scope.parentElement) {
          ancestors.push({ tag: scope.tagName, role: safeRole(scope), evidence: check(() => ({ relationship_labels: relations(scope).map(item => item.degree), profile_links: profileLinks(scope).length })) });
        }
        return { excluded: excluded(link), filter: check(() => { C.searchContext(link.href); return {}; }), ancestors };
      }),
      results: check(() => {
        const context = C.searchContext(location.href);
        const cards = resultCards(content());
        return { recognized_rows: cards.length, degrees: cards.reduce((counts, card) => { counts[card.record.degree] = (counts[card.record.degree] || 0) + 1; return counts; }, {}), pagination: check(() => ({ next: pagination(content(), context.page).next })) };
      }),
    };
  }
  root.TinPageEvidence = Object.freeze({ text, visible, label, excluded, content, selectedProfile, account, resultCards, pagination, diagnostics });
})(globalThis);
