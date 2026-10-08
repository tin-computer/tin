(function attachTinLinkedInProtocol(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.TinLinkedInProtocol = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildProtocol() {
  "use strict";

  const VERSION = 2;
  const DASHBOARD_SOURCE = "tin.dashboard.linkedin-session.v2";
  const EXTENSION_SOURCE = "tin.linkedin.session.v2";
  const REQUEST_TYPES = new Set([
    "CONNECT_SESSION",
    "REFRESH",
    "STATUS",
    "DISCONNECT",
  ]);
  const DASHBOARD_ORIGINS = new Set([
    "https://tin.computer",
    "https://www.tin.computer",
    "https://staging.tin.computer",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
  ]);
  const API_BASES = new Set([
    "https://api.tin.computer",
    "https://api.staging.tin.computer",
    "http://localhost:18080",
    "http://127.0.0.1:18080",
  ]);
  const STATUS_VALUES = new Set([
    "not_connected",
    "connecting",
    "connected",
    "refreshing",
    "signed_out",
    "checkpoint",
    "needs_reconnect",
    "disconnect_pending",
    "error",
  ]);

  class ProtocolError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "ProtocolError";
      this.code = code;
    }
  }

  function isPlainObject(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return false;
    }
    const proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
  }

  function exactKeys(value, allowed, label) {
    if (!isPlainObject(value)) {
      throw new ProtocolError("invalid_request", `${label} must be an object`);
    }
    const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
    if (unknown.length) {
      throw new ProtocolError(
        "invalid_request",
        `${label} contains unsupported fields: ${unknown.join(", ")}`,
      );
    }
  }

  function requiredString(value, label, maxLength) {
    if (typeof value !== "string" || !value.trim() || value.length > maxLength) {
      throw new ProtocolError("invalid_request", `${label} is invalid`);
    }
    return value.trim();
  }

  function normalizeApiBase(value) {
    const base = requiredString(value, "api_base", 200).replace(/\/+$/, "");
    if (!API_BASES.has(base)) {
      throw new ProtocolError("api_base_not_allowed", "Tin API origin is not allowed");
    }
    return base;
  }

  function isAllowedDashboardOrigin(origin) {
    return DASHBOARD_ORIGINS.has(origin);
  }

  function validateRequest(value) {
    exactKeys(
      value,
      ["source", "protocol_version", "type", "request_id", "payload"],
      "request",
    );
    if (value.source !== DASHBOARD_SOURCE || value.protocol_version !== VERSION) {
      throw new ProtocolError("invalid_request", "Unsupported bridge protocol");
    }
    if (!REQUEST_TYPES.has(value.type)) {
      throw new ProtocolError("invalid_request", "Unsupported bridge request type");
    }
    const requestId = requiredString(value.request_id, "request_id", 128);
    if (!/^[A-Za-z0-9._:-]+$/.test(requestId)) {
      throw new ProtocolError("invalid_request", "request_id is invalid");
    }
    const payload = value.payload === undefined ? {} : value.payload;
    if (value.type === "CONNECT_SESSION") {
      exactKeys(
        payload,
        ["api_base", "project_id", "session_grant"],
        "CONNECT_SESSION payload",
      );
      return {
        type: value.type,
        request_id: requestId,
        payload: {
          api_base: normalizeApiBase(payload.api_base),
          project_id: requiredString(payload.project_id, "project_id", 256),
          session_grant: requiredString(payload.session_grant, "session_grant", 8192),
        },
      };
    }
    exactKeys(payload, [], `${value.type} payload`);
    return { type: value.type, request_id: requestId, payload: {} };
  }

  function cleanString(value, maxLength) {
    return typeof value === "string" && value.length
      ? value.slice(0, maxLength)
      : null;
  }

  function sanitizeStatus(value) {
    if (!isPlainObject(value)) {
      return {
        status: "not_connected",
        connected: false,
        project_id: null,
        actor: null,
        last_synced_at: null,
        status_code: null,
        extension_version: null,
      };
    }
    const status = STATUS_VALUES.has(value.status) ? value.status : "error";
    const actor = isPlainObject(value.actor)
      ? {
          display_name: cleanString(value.actor.display_name, 200),
          profile_url: cleanString(value.actor.profile_url, 500),
        }
      : null;
    return {
      status,
      connected: status === "connected",
      project_id: cleanString(value.project_id, 256),
      actor,
      last_synced_at: cleanString(value.last_synced_at, 64),
      status_code: cleanString(value.status_code, 128),
      extension_version: cleanString(value.extension_version, 64),
    };
  }

  function responseEnvelope(request, result) {
    const ok = Boolean(result && result.ok);
    return {
      source: EXTENSION_SOURCE,
      protocol_version: VERSION,
      type: "RESULT",
      request_type: request.type,
      request_id: request.request_id,
      ok,
      ...(ok
        ? { payload: sanitizeStatus(result.payload) }
        : {
            error: {
              code: cleanString(result?.error?.code, 128) || "extension_error",
              message:
                cleanString(result?.error?.message, 300) ||
                "The extension could not complete that request.",
            },
          }),
    };
  }

  return Object.freeze({
    VERSION,
    DASHBOARD_SOURCE,
    EXTENSION_SOURCE,
    DASHBOARD_ORIGINS: Object.freeze([...DASHBOARD_ORIGINS]),
    API_BASES: Object.freeze([...API_BASES]),
    ProtocolError,
    isAllowedDashboardOrigin,
    normalizeApiBase,
    responseEnvelope,
    sanitizeStatus,
    validateRequest,
  });
});
