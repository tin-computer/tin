/* This bridge returns connection status only. It never returns a device bearer. */
(function () {
  "use strict";
  const NS = "tin.linkedin.collection.v3";
  function receive(event) {
    const request = event.data;
    if (event.source !== window || event.origin !== location.origin || request?.source !== "tin.dashboard.collection.v3" || !["PAIR", "DISCOVER", "WAKE"].includes(request.type) || typeof request.id !== "string" || request.id.length > 100 || (request.type === "PAIR" && (typeof request.grant !== "string" || request.grant.length > 128))) return;
    // Reloading/removing an extension leaves its old content script in open tabs.
    // Retire it silently: a newly injected bridge may be handling the same request.
    // If there is no live bridge, the dashboard timeout asks the user to refresh.
    const retire = () => window.removeEventListener("message", receive);
    const runtime = globalThis.chrome?.runtime;
    if (!runtime?.id || typeof runtime.sendMessage !== "function") { retire(); return; }
    try { runtime.sendMessage({ namespace: NS, type: request.type, base: location.origin, grant: request.grant }, result => {
      const error = runtime.lastError;
      if (!globalThis.chrome?.runtime?.id) { retire(); return; }
      const payload = result?.payload;
      window.postMessage({ source: NS, id: request.id, ok: !error && result?.ok === true,
        error: result?.error || (error ? "extension_unavailable" : undefined),
        payload: request.type === "DISCOVER" && payload ? {version:payload.version,protocol:payload.protocol,project_id:payload.project_id,account:payload.account,reason:payload.reason,cloud_available:payload.cloud_available === true,session_available:payload.session_available === true} : payload ? { connected: payload.connected === true, project_id: payload.project_id, actor: payload.actor } : undefined }, location.origin);
    }); } catch { retire(); }
  }
  window.addEventListener("message", receive);
})();
