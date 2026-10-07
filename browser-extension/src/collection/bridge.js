/* This bridge returns connection status only. It never returns a device bearer. */
(function () {
  "use strict";
  const NS = "tin.linkedin.collection.v3";
  window.addEventListener("message", event => {
    const request = event.data;
    if (event.source !== window || event.origin !== location.origin || request?.source !== "tin.dashboard.collection.v3" || request.type !== "PAIR" || typeof request.id !== "string" || request.id.length > 100 || typeof request.grant !== "string" || request.grant.length > 128) return;
    chrome.runtime.sendMessage({ namespace: NS, type: "PAIR", base: location.origin, grant: request.grant }, result => {
      const error = chrome.runtime.lastError;
      const payload = result?.payload;
      window.postMessage({ source: NS, id: request.id, ok: !error && result?.ok === true,
        error: result?.error || (error ? "extension_unavailable" : undefined),
        payload: payload ? { connected: payload.connected === true, project_id: payload.project_id, actor: payload.actor } : undefined }, location.origin);
    });
  });
})();
