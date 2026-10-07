(function installTinLinkedInBridge() {
  "use strict";

  const Protocol = globalThis.TinLinkedInProtocol;
  if (!Protocol || !Protocol.isAllowedDashboardOrigin(window.location.origin)) return;

  function post(value) {
    window.postMessage(value, window.location.origin);
  }

  function sendToBackground(message) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(message, (response) => {
        const runtimeError = chrome.runtime.lastError;
        if (runtimeError) {
          resolve({
            ok: false,
            error: {
              code: "extension_unavailable",
              message: "The Tin extension background service is unavailable.",
            },
          });
          return;
        }
        resolve(
          response && typeof response === "object"
            ? response
            : {
                ok: false,
                error: {
                  code: "invalid_extension_response",
                  message: "The Tin extension returned an invalid response.",
                },
              },
        );
      });
    });
  }

  window.addEventListener("message", async (event) => {
    if (
      event.source !== window ||
      event.origin !== window.location.origin ||
      !event.data ||
      event.data.source !== Protocol.DASHBOARD_SOURCE
    ) {
      return;
    }

    let request;
    try {
      request = Protocol.validateRequest(event.data);
    } catch (error) {
      const fallback = {
        type:
          typeof event.data.type === "string" ? event.data.type : "INVALID_REQUEST",
        request_id:
          typeof event.data.request_id === "string"
            ? event.data.request_id.slice(0, 128)
            : "invalid",
      };
      post(
        Protocol.responseEnvelope(fallback, {
          ok: false,
          error: {
            code: error?.code || "invalid_request",
            message: error?.message || "The bridge request is invalid.",
          },
        }),
      );
      return;
    }

    const result = await sendToBackground({
      namespace: Protocol.EXTENSION_SOURCE,
      protocol_version: Protocol.VERSION,
      type: request.type,
      request_id: request.request_id,
      payload: request.payload,
    });

    // Apply the public status schema again at the page boundary. LinkedIn
    // cookies and the extension's device bearer never become page-visible,
    // even if a future background response accidentally includes them.
    if (result?.ok) {
      result.payload = Protocol.sanitizeStatus(result.payload);
    }
    post(Protocol.responseEnvelope(request, result));
  });

  post({
    source: Protocol.EXTENSION_SOURCE,
    protocol_version: Protocol.VERSION,
    type: "READY",
    payload: {
      extension_version: chrome.runtime.getManifest().version,
    },
  });
})();
