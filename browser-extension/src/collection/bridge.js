/* This bridge returns connection status only. It never returns a device bearer. */
(function () {
  "use strict";
  const NS = "tin.linkedin.collection.v3";
  window.addEventListener("message", event => {
    const request = event.data;
    if (event.source !== window || event.origin !== location.origin || request?.source !== "tin.dashboard.collection.v3" || !["PAIR", "DISCOVER", "WAKE"].includes(request.type) || typeof request.id !== "string" || request.id.length > 100 || (request.type === "PAIR" && (typeof request.grant !== "string" || request.grant.length > 128))) return;
    chrome.runtime.sendMessage({ namespace: NS, type: request.type, base: location.origin, grant: request.grant }, result => {
      const error = chrome.runtime.lastError;
      const payload = result?.payload;
      window.postMessage({ source: NS, id: request.id, ok: !error && result?.ok === true,
        error: result?.error || (error ? "extension_unavailable" : undefined),
        payload: request.type === "DISCOVER" && payload ? {version:payload.version,protocol:payload.protocol,project_id:payload.project_id,account:payload.account,reason:payload.reason,cloud_available:payload.cloud_available === true,session_available:payload.session_available === true} : payload ? { connected: payload.connected === true, project_id: payload.project_id, actor: payload.actor } : undefined }, location.origin);
    });
  });
})();
