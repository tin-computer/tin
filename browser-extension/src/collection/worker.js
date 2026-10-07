/* Project-bound collection transport. Raw sessions never enter a content script. */
"use strict";
importScripts("collection/core.js");
(function () {
  const C = globalThis.TinCollectorCore;
  const KEY = "tin.linkedin.collection.v3", ALARM = "tin.linkedin.collection.v3";
  const NS = "tin.linkedin.collection.v3";
  const origins = new Set(["https://tin.computer", "https://www.tin.computer", "https://app.tin.computer", "https://staging.tin.computer", "http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:18080", "http://127.0.0.1:18080", "http://localhost:3000"]);
  let running = false, timer, operations = Promise.resolve();
  const serialize = fn => { const result = operations.then(fn); operations = result.catch(() => {}); return result; };
  const ready = chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  const load = async () => (await chrome.storage.local.get(KEY))[KEY] || {};
  const save = state => chrome.storage.local.set({ [KEY]: state });
  const actor = value => ({ key: value.key, name: value.name || "", profile_url: value.profile_url || null });
  const publicState = state => ({ connected: Boolean(state.bearer), project_id: state.project_id,
    status: state.status || "not_connected", reason: state.reason || "", run_id: state.job?.run_id,
    friend_index: state.job?.friend_index, page: state.job?.next_page, actor: state.actor?.name || "",
    permission: state.connection?.permission || null, session_available: state.connection?.session_available === true });
  async function api(state, path, payload) {
    if (!origins.has(state.base)) throw Error("untrusted_origin");
    const response = await fetch(state.base + path, {
      method: payload === undefined ? "GET" : "POST", credentials: "omit", redirect: "error",
      headers: { "Content-Type": "application/json", "X-Tin-Extension-Id": chrome.runtime.id,
        ...(state.bearer ? { Authorization: `Bearer ${state.bearer}` } : {}) },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }), signal: AbortSignal.timeout(20_000),
    });
    const data = await response.json();
    if (!response.ok) {
      // Only finite backend errors are displayed; arbitrary upstream bodies stay private.
      const code = typeof data.detail === "string" && /^[a-z_]{1,60}$/.test(data.detail) ? data.detail : "backend_unavailable";
      throw Error(code);
    }
    return data;
  }
  const route = state => `/api/projects/${state.project_id}/connection-extension`;
  const command = (state, op, payload) => api(state, `${route(state)}/${state.job.run_id}/${op}`, payload);
  const fence = state => ({ generation: state.job.generation, lease: state.lease });
  function schedule() { clearTimeout(timer); timer = setTimeout(() => void serialize(tick), 3000); }
  async function navigate(state, url, phase) {
    // Persist navigation intent before changing the tab. A worker restart can replay
    // this URL without interpreting the previous page as a changed collection view.
    state.navigation = { url, phase };
    await save(state);
    await chrome.tabs.update(state.tab_id, { url });
    state.phase = phase;
    delete state.navigation;
    await save(state);
  }
  async function pageCall(tabId, method, input) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["src/collection/core.js", "src/collection/page-evidence.js", "src/collection/linkedin.js"] });
    const [result] = await chrome.scripting.executeScript({ target: { tabId }, func: async (method, input) => {
      try {
        if (method === "account") return { ok: true, value: globalThis.TinPageEvidence.account() };
        if (!["profile", "snapshot", "searchContract", "diagnostics"].includes(method)) throw Error("invalid_operation");
        return { ok: true, value: await globalThis.TinLinkedIn[method](input) };
      } catch (error) { return { ok: false, code: error.message }; }
    }, args: [method, input || null] });
    if (!result?.result?.ok) throw Error(result?.result?.code || "browser_unavailable");
    return result.result.value;
  }
  async function linkedInTab() {
    const tabs = await chrome.tabs.query({ url: "https://www.linkedin.com/*" });
    const tab = tabs.find(t => t.active) || tabs[0];
    if (!tab) throw Error("open_linkedin_tab");
    return tab;
  }
  async function pair(base, grant) {
    if (!origins.has(base)) throw Error("untrusted_origin");
    const old = await load();
    if (["collecting","cloud_running"].includes(old.status)) throw Error("collection_active");
    if (old.status === "migration_pending") {
      try { await globalThis.TinLinkedInRetireLegacy(); } catch { throw Error("legacy_retirement_pending"); }
      old.status="ready";delete old.pairing;await save(old);return publicState(old);
    }
    const tab = await linkedInTab(), identity = actor(await pageCall(tab.id, "account"));
    // Persist the same generated bearer before upload so an uncertain response is retryable.
    const pending = old.pairing?.grant === grant ? old.pairing : { grant, bearer: crypto.randomUUID() + crypto.randomUUID(), actor: identity };
    await save({ ...old, pairing: pending });
    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(pending.bearer)))].map(b => b.toString(16).padStart(2,"0")).join("");
    const result = await api({ base }, "/api/connection-extension/pair", { grant, token_hash: hash, actor: pending.actor });
    const state = { base, bearer: pending.bearer, actor: pending.actor, project_id: result.project_id, pairing: pending, status: "migration_pending" };
    await save(state);
    try { await globalThis.TinLinkedInRetireLegacy(); } catch { throw Error("legacy_retirement_pending"); }
    state.status="ready";delete state.pairing;await save(state);
    await chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
    schedule();
    return publicState(state);
  }
  async function discover() {
    let account = null, reason = "";
    try { const tab = await linkedInTab(); account = actor(await pageCall(tab.id, "account")); }
    catch (error) { reason = error.message; }
    const state = await load();
    let projectId = null;
    if (state.bearer) {
      try { await connection(state); projectId = state.project_id; } catch { /* Re-pair an expired device. */ }
    }
    const setupReason = reason || (!state.connection?.session_available ? state.reason : "") || "";
    return { version: chrome.runtime.getManifest().version, protocol: 4, account, reason:!setupReason || /^[a-z_]{1,60}$/.test(setupReason) ? setupReason : "browser_unavailable", project_id:projectId,
      cloud_available: state.connection?.cloud_available === true, session_available:state.connection?.session_available === true };
  }
  async function connection(state) {
    state.connection = await api(state, `${route(state)}/status`);
    await save(state);
    return state.connection;
  }
  async function capture(state, accountTab) {
    const contextKey = `tin.linkedin.collection.context.${accountTab.id}`;
    const readContext = async () => {
      const stored = await chrome.storage.local.get([contextKey,"tin.linkedin.collection.context.v3"]);
      return stored[contextKey] || stored["tin.linkedin.collection.context.v3"];
    };
    let observed = await readContext();
    if (!observed || observed.tab_id !== accountTab.id || Date.now() - observed.at > 120000) {
      const after = Date.now();
      await chrome.tabs.reload(accountTab.id);
      const until = Date.now() + 12000;
      do {
        await new Promise(resolve => setTimeout(resolve, 300));
        observed = await readContext();
        if (observed?.tab_id === accountTab.id && observed.at >= after) break;
      } while (Date.now() < until);
      if (!observed || observed.tab_id !== accountTab.id || observed.at < after) throw Error("refresh_linkedin_context");
    }
    const allow = new Set(["li_at", "JSESSIONID", "bcookie", "bscookie", "lidc", "lang"]);
    const cookies = async () => (await chrome.cookies.getAll({domain:"linkedin.com"}))
      .filter(c => allow.has(c.name)).map(c => ({name:c.name,value:c.value,domain:c.domain,path:c.path,
        secure:c.secure,http_only:c.httpOnly,same_site:c.sameSite,...(c.expirationDate?{expiration_date:c.expirationDate}:{})}))
      .sort((a,b) => `${a.name}:${a.domain}:${a.path}`.localeCompare(`${b.name}:${b.domain}:${b.path}`));
    const first = await cookies();
    if ((await pageCall(accountTab.id, "account")).key !== state.actor.key) throw Error("account_changed");
    if (JSON.stringify(first) !== JSON.stringify(await cookies())) throw Error("session_changed_during_capture");
    return {cookies:first,user_agent:navigator.userAgent,browser_context:observed.context};
  }
  async function refreshSession(state) {
    const current = state.connection || await connection(state);
    if (!current.permission?.version || current.permission.mode === "local_only" || !current.cloud_available) return;
    if (state.refresh_blocked) return;
    if (state.refresh_after && Date.now() < state.refresh_after) return;
    let query = (await chrome.storage.local.get("tin.linkedin.collection.search"))["tin.linkedin.collection.search"];
    const untilExpiry = Date.parse(current.session_expires_at || "") - Date.now();
    if (current.session_available && untilExpiry > 86400000 && (!query || state.session_query_id === query.id)) return;
    const accountTab = await linkedInTab();
    if ((await pageCall(accountTab.id, "account")).key !== state.actor.key) throw Error("account_changed");
    const context = (await chrome.storage.local.get("tin.linkedin.collection.context.v3"))["tin.linkedin.collection.context.v3"];
    let preparedTab;
    try {
      // Setup prepares the supported search contract once, with no friend hardcoded.
      if (!query?.client_version || query.client_version !== context?.context?.li_track?.clientVersion || Date.now() - query.at > 86400000) {
        preparedTab = await chrome.tabs.create({url:"about:blank",active:false});
        await chrome.tabs.update(preparedTab.id,{url:'https://www.linkedin.com/search/results/people/?network=%5B%22S%22%5D'});
        const deadline = Date.now() + 15000;
        do {
          await new Promise(resolve => setTimeout(resolve, 300));
          query = (await chrome.storage.local.get("tin.linkedin.collection.search"))["tin.linkedin.collection.search"];
          if (query?.tab_id === preparedTab.id) break;
          if ((await chrome.tabs.get(preparedTab.id)).status !== "complete") continue;
          // A server-rendered or cached first page need not issue a GraphQL request.
          // Keep the query and browser context bound to this same setup tab.
          let evidence;
          try { evidence = await pageCall(preparedTab.id, "searchContract"); }
          catch (error) {
            if (["wrong_page","account_unavailable"].includes(error.message)) continue;
            throw error;
          }
          if (evidence.actor.key !== state.actor.key) throw Error("account_changed");
          const contextKey = `tin.linkedin.collection.context.${preparedTab.id}`;
          const observed = (await chrome.storage.local.get(contextKey))[contextKey];
          state.setup_diagnostics = {...evidence.diagnostics,context_present:Boolean(observed?.context)};
          if (evidence.query_id && observed?.context && Date.now() - observed.at < 120000) {
            query = {id:evidence.query_id,at:Date.now(),tab_id:preparedTab.id,client_version:observed.context.li_track.clientVersion};
            await chrome.storage.local.set({"tin.linkedin.collection.search":query,[`tin.linkedin.collection.query.${preparedTab.id}`]:query.id});
            break;
          }
        } while (Date.now() < deadline);
        // Login verification does not require search results. If this initial view
        // exposes no search contract, a run prepares its selected friend in Chrome.
        // The backend already pauses cloud execution until a valid source is ready.
        if (query?.tab_id !== preparedTab.id) query = null;
      }
      const session = await capture(state, query && preparedTab ? preparedTab : accountTab);
      if (query && query.client_version !== session.browser_context.li_track.clientVersion) throw Error("refresh_linkedin_context");
      state.connection = await api(state, `${route(state)}/session`, {actor_key:state.actor.key,
        session, query_id:query?.id || null, expected_generation:current.session_generation || null});
      state.session_query_id = query?.id || null;
      state.reason = "";
      delete state.refresh_after;
      delete state.setup_diagnostics;
      await save(state);
    } finally {
      if (preparedTab) await chrome.tabs.remove(preparedTab.id).catch(() => {});
    }
  }
  async function begin(consent = false) {
    let state = await load();
    if (state.status === "migration_pending") throw Error("legacy_retirement_pending");
    const pending = await api(state, `${route(state)}/pending`);
    if (!pending) { state.status = "ready"; state.reason = "no_pending_collection"; await save(state); return publicState(state); }

    if (pending.state === "cloud_ready" || (pending.execution_mode === "cloud" && pending.state === "collecting")) {
      state.job = pending; state.status = "cloud_running"; await save(state); return publicState(state);
    }
    const accountTab = await linkedInTab();
    const identity = actor(await pageCall(accountTab.id, "account"));
    if (identity.key !== state.actor.key || identity.key !== pending.actor.key) throw Error("account_changed");
    state.job = pending;
    if (pending.state === "paused") { await command(state, "resume", { actor_key: identity.key }); return begin(consent); }
    if (["http_v1","http_v2"].includes(pending.cloud_transport) && pending.execution_mode !== "cloud" && pending.state !== "handoff_pending" && !consent && !state.connection?.permission?.version) throw Error("cloud_transfer_consent_required");
    const claim = await command(state, "claim", { actor_key: identity.key });
    const tab = await chrome.tabs.create({ url: "about:blank", active: false });
    state = { ...state, job: claim, lease: claim.lease, tab_id: tab.id, status: "collecting", phase: "opening_profile", reason: "" };
    delete state.job.lease;
    await save(state);
    if (claim.cloud_transport === "http_v1") {
      const session = await capture(state, accountTab);
      await command(state,"session",{ ...fence(state),session });
    }
    schedule(); return publicState(state);
  }
  function source(selection, url) {
    return { friend_url: selection.friend.profile_url, friend_name: selection.friend.name,
      actor: actor(selection.actor), first_degree: true, collection_url: url, query_id: null };
  }
  const stopReason = code => ({ login_or_checkpoint: "challenge", platform_limit: "rate_limited",
    page_changed_during_read: "page_changed", missing_friend_filter: "filters_changed",
    ambiguous_friend_filter: "filters_changed", account_unavailable: "browser_unavailable",
    account_evidence_unavailable: "browser_unavailable" })[code]
    || (new Set(["account_changed","filters_changed","page_changed","repeated_page","people_limit","friend_time_limit"]).has(code) ? code : "unsupported_layout");
  async function tick() {
    if (running) return;
    running = true;
    let state;
    try {
      await ready; state = await load();
      if (state.status === "cloud_running") {
        const pending = await api(state, `${route(state)}/pending`);
        if (pending?.state === "handoff_pending") { await begin(); return; }
        if (pending?.state === "waiting_browser") {
          state.status = "ready";state.job = pending;await save(state);schedule();return;
        }
        if (!pending) state.status = "finished";
        else {
          state.job = pending;
          if (pending.state === "paused") { state.status = "paused"; state.reason = pending.reason; }
        }
        await save(state);
        return;
      }
      if (state.status !== "collecting") {
        if (!state.bearer || state.status === "migration_pending") return;
        await connection(state);
        if (!state.connection.permission?.version) return;
        const pending = await api(state, `${route(state)}/pending`);
        if (pending?.state === "paused") {
          state.job = pending; state.status = "paused"; state.reason = pending.reason;
          if (state.tab_id && !state.page_diagnostics) {
            try { state.page_diagnostics = await pageCall(state.tab_id, "diagnostics"); }
            catch { /* A closed or unavailable tab cannot supply structural evidence. */ }
          }
          if (state.refresh_requested) {
            delete state.refresh_requested; await save(state);
            await refreshSession(state);
            if (["session_expired","cloud_permission_required"].includes(pending.reason) && state.connection.session_available) {
              await command(state,"resume",{actor_key:state.actor.key});state.status="ready";schedule();
            }
          }
          await save(state); return;
        }
        if (pending?.state === "cloud_ready" || (pending?.execution_mode === "cloud" && pending.state === "collecting")) {
          state.job = pending; state.status = "cloud_running"; await save(state); return;
        }
        delete state.refresh_requested;
        // Never refresh a session while this worker owns a collection attempt.
        try { await refreshSession(state); }
        catch (error) {
          if (["account_changed","session_expired","challenge","rate_limited","access_denied","unsupported_identity"].includes(error.message)) {
            throw error;
          }
          state.reason = error.message; state.refresh_after = Date.now() + 300000; await save(state);
        }
        if (pending && pending.run_id !== state.stopped_run_id) {
          if (pending.cloud_transport === "http_v2" && state.connection.session_available && pending.reason !== "browser_preparation_required" && pending.state === "waiting_browser") { schedule(); return; }
          await begin();
        }
        return;
      }
      if (state.pendingSource) {
        state.job = await command(state,"source",state.pendingSource);
        delete state.pendingSource;
        state.phase="acknowledged";
        if (state.job.state === "cloud_ready" || state.job.execution_mode === "cloud" || Object.keys(state.job.sources || {}).length === state.job.inputs.friends.length) state.status="cloud_running";
        await save(state); schedule(); return;
      }
      if (state.pending) {
        // Replay the exact body, including observation time, until acknowledged.
        state.job = await command(state, "page", state.pending);
        delete state.pending;
        state.phase = "acknowledged";
        await save(state);
      } else {
        await command(state, "heartbeat", fence(state));
      }
      if (["completed", "partial", "failed", "stopped"].includes(state.job.state)) {
        state.status = state.job.state; await save(state); return;
      }
      const tab = await chrome.tabs.get(state.tab_id);
      if (tab.status === "loading") { schedule(); return; }
      if (state.navigation) {
        await navigate(state,state.navigation.url,state.navigation.phase);
        schedule(); return;
      }
      if (state.phase === "opening_profile") {
        await navigate(state,state.job.inputs.friends[state.job.friend_index],"profile");
        schedule(); return;
      }
      if (state.phase === "acknowledged") {
        // Advance from the server's checkpoint, never by a blind second Next click.
        if (state.selection?.friend.profile_url !== state.job.inputs.friends[state.job.friend_index]) {
          delete state.selection;
          await navigate(state,state.job.inputs.friends[state.job.friend_index],"profile");
        } else {
          const url = new URL(state.selection.collection_url);
          url.searchParams.set("page", String(state.job.next_page));
          await navigate(state,url.href,"read");
        }
        schedule(); return;
      }
      if (state.phase === "profile") {
        const selection = C.prepareSelection(await pageCall(state.tab_id, "profile"), state.job.inputs.keywords);
        if (selection.actor.key !== state.actor.key) throw Error("account_changed");
        if (selection.friend.profile_url !== state.job.inputs.friends[state.job.friend_index]) throw Error("friend_changed");
        state.selection = selection;
        const url = new URL(selection.collection_url);
        url.searchParams.set("page", String(state.job.next_page));
        await navigate(state,url.href,"read"); schedule(); return;
      }
      const run = C.createRun(state.selection);
      const snapshot = await pageCall(state.tab_id, "snapshot", run);
      C.checkSnapshot(run, snapshot);
      if (snapshot.page !== state.job.next_page) throw Error("page_changed");
      if (!["next", "end"].includes(snapshot.next)) throw Error("unsupported_pagination");
      if (snapshot.records.some(p => p.degree !== "2nd")) throw Error("filters_changed");
      if (["http_v1", "http_v2"].includes(state.job.cloud_transport)) {
        // Only the observed query identifier is retained, never the request headers/URL.
        const queryKey = `tin.linkedin.collection.query.${state.tab_id}`;
        let queryId = (await chrome.storage.local.get(queryKey))[queryKey];
        if (!queryId) {
          const evidence = await pageCall(state.tab_id, "searchContract");
          if (evidence.actor.key !== state.actor.key) throw Error("account_changed");
          queryId = evidence.query_id;
        }
        if (!queryId) throw Error("unsupported_search_contract");
        const prepared = source(state.selection,snapshot.url); prepared.query_id = queryId;
        state.pendingSource={ ...fence(state),source:prepared };
        await save(state); schedule(); return;
      }
      const people = snapshot.records.map(p => ({ profile_url: p.profile_url, name: p.name,
        headline: p.headline || "", location: p.location || "", visible_text: p.visible_text || "", degree: "2nd" }));
      state.pending = { ...fence(state), friend_index: state.job.friend_index, page: snapshot.page,
        actor_key: state.actor.key, source: source(state.selection, snapshot.url), people,
        next_page: snapshot.next === "next", observed_at: snapshot.observed_at };
      await save(state); schedule();
    } catch (error) {
      if (!state) return;
      if (["account_changed","session_expired","challenge","rate_limited","access_denied","unsupported_identity"].includes(error.message)) state.refresh_blocked = true;
      const transient = ["Failed to fetch", "fetch failed", "backend_unavailable", "The operation was aborted due to timeout"].some(c => error.message.includes(c));
      if (transient) { schedule(); return; }
      const reason = stopReason(error.message);
      if (state.tab_id && !state.page_diagnostics) {
        try { state.page_diagnostics = await pageCall(state.tab_id, "diagnostics"); }
        catch { /* Never replace the original failure with a diagnostic failure. */ }
      }
      try { if (state.lease) await command(state, "pause", { ...fence(state), reason }); } catch { /* Fenced or disconnected: no new browser actions. */ }
      state.status = "paused"; state.reason = error.message;
      await save(state);
    } finally { running = false; }
  }
  async function handle(message, sender) {
    await ready;
    if (["PAIR", "DISCOVER", "WAKE"].includes(message.type)) {
      const origin = sender.url ? new URL(sender.url).origin : "";
      if (!sender.tab || !origins.has(origin) || message.base !== origin) throw Error("untrusted_origin");
      if (message.type === "DISCOVER") return discover();
      if (message.type === "WAKE") { const state = await load();delete state.refresh_after;delete state.refresh_blocked;state.reason="";state.refresh_requested=true;await save(state);schedule();return publicState(state); }
      return pair(origin, message.grant);
    }
    if (sender.url !== chrome.runtime.getURL("popup/collection.html")) throw Error("untrusted_sender");
    if (message.type === "STATUS") return publicState(await load());
    if (message.type === "BEGIN") return begin(message.cloud_consent === true);
    if (message.type === "RESUME") { const state = await load(); if (state.status !== "collecting") return begin(message.cloud_consent === true); schedule(); return publicState(state); }
    if (message.type === "STOP") {
      const state = await load();
      if (state.job) await command(state, "stop", {});
      state.status = "stopped"; state.stopped_run_id = state.job?.run_id; await save(state); return publicState(state);
    }
    throw Error("invalid_operation");
  }
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (message?.namespace !== NS) return false;
    (message.type === "STATUS" ? handle(message,sender) : serialize(() => handle(message, sender))).then(payload => respond({ ok: true, payload }), error => respond({ ok: false, error: /^[a-z_]{1,60}$/.test(error.message) ? error.message : "extension_unavailable" }));
    return true;
  });
  chrome.webRequest.onBeforeSendHeaders.addListener(details => {
    const fields={};
    for(const header of details.requestHeaders || []) {
      const key={"accept-language":"accept_language","x-li-lang":"li_lang","x-li-track":"li_track"}[header.name.toLowerCase()];
      if(key) fields[key]=header.value;
    }
    const context=TinLinkedInBrowserContext.normalize(fields);
    if(context && details.tabId >= 0) {
      const observed = {at:Date.now(),tab_id:details.tabId,context};
      void chrome.storage.local.set({"tin.linkedin.collection.context.v3":observed,[`tin.linkedin.collection.context.${details.tabId}`]:observed});
    }
    if (!details.url) return;
    let query;
    try { const url = new URL(details.url); query = url.searchParams.get("queryId"); } catch { return; }
    if (!/^voyagerSearchDashClusters\.[a-f0-9]{20,64}$/.test(query || "")) return;
    void chrome.storage.local.set({ [`tin.linkedin.collection.query.${details.tabId}`]: query,
      ...(context ? {"tin.linkedin.collection.search": {id:query,at:Date.now(),tab_id:details.tabId,client_version:context.li_track.clientVersion}} : {}) });
  }, { urls:["https://www.linkedin.com/voyager/api/*"] }, ["requestHeaders","extraHeaders"]);
  chrome.tabs.onRemoved.addListener(tabId => void chrome.storage.local.remove([`tin.linkedin.collection.context.${tabId}`,`tin.linkedin.collection.query.${tabId}`]));
  chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void serialize(tick); });
  chrome.runtime.onStartup.addListener(() => void serialize(tick));
  // Chrome may clear alarms on extension updates. Re-establish wakeups from durable state.
  void ready.then(async () => { const state = await load(); if (state.bearer) { await chrome.alarms.create(ALARM,{periodInMinutes:0.5}); schedule(); } });
})();
