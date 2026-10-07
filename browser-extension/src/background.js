/* global TinLinkedInBrowserContext, TinLinkedInProtocol */
"use strict";

importScripts("protocol.js", "browser-context.js");

(function installLinkedInSessionWorker() {
  const Protocol = TinLinkedInProtocol;
  const SESSION_KEY = "tin.linkedin.session.v2";
  const BROWSER_CONTEXT_KEY = "tin.linkedin.browser-context.v1";
  const REFRESH_ALARM = "tin.linkedin.session-refresh.v2";
  const COOKIE_REFRESH_ALARM = "tin.linkedin.cookie-refresh.v2";
  const DISCONNECT_ALARM = "tin.linkedin.disconnect-retry.v2";
  const POPUP_NAMESPACE = "tin.linkedin.popup.v2";
  const REFRESH_MINUTES = 30;
  const COOKIE_REFRESH_DELAY_MINUTES = 0.1;
  const DISCONNECT_RETRY_MINUTES = 5;
  const DEVICE_ROTATION_DETAIL = "LinkedIn device credential must rotate";
  const VALIDATION_FAILURE_CODE = "linkedin_session_validation_failed";
  const VALIDATION_REASONS = new Set([
    "checkpoint_required",
    "linkedin_unavailable",
    "member_not_found",
    "network_error",
    "rate_limited",
    "request_rejected",
    "session_forbidden",
    "session_invalid",
    "unexpected_response",
    "unknown",
  ]);
  const VALIDATION_CATEGORIES = new Set([
    "auth_redirect",
    "auth_rejected",
    "checkpoint_redirect",
    "checkpoint_response",
    "client_rejected",
    "csrf_rejected",
    "not_found",
    "rate_limited",
    "request_contract",
    "signed_out_response",
    "unexpected_payload",
    "upstream_failure",
    "unknown",
  ]);
  const VALIDATION_RESPONSE_KINDS = new Set([
    "empty",
    "html",
    "json",
    "other",
    "text",
    "unknown",
  ]);
  const COOKIE_NAMES = Object.freeze([
    "li_at",
    "JSESSIONID",
    "bcookie",
    "bscookie",
    "lidc",
    "lang",
  ]);
  const COOKIE_NAME_SET = new Set(COOKIE_NAMES);
  let sessionOperation = Promise.resolve();
  const browserContext = TinLinkedInBrowserContext.create({
    chromeApi: chrome,
    storageKey: BROWSER_CONTEXT_KEY,
  });

  class SessionError extends Error {
    constructor(code, message, status = null) {
      super(message);
      this.name = "SessionError";
      this.code = code;
      this.status = status;
    }
  }

  function serializeSessionOperation(operation) {
    const result = sessionOperation.then(operation, operation);
    // Keep the queue usable after an individual caller observes a rejection.
    sessionOperation = result.catch(() => undefined);
    return result;
  }

  function manifestVersion() {
    return chrome.runtime.getManifest().version;
  }

  function publicStatus(state) {
    return Protocol.sanitizeStatus({
      status: state?.status || "not_connected",
      project_id: state?.project_id,
      actor: state?.actor,
      last_synced_at: state?.last_synced_at,
      status_code: state?.status_code,
      extension_version: manifestVersion(),
    });
  }

  async function loadSession() {
    const stored = await chrome.storage.local.get(SESSION_KEY);
    const value = stored?.[SESSION_KEY];
    return value && typeof value === "object" ? value : null;
  }

  async function saveSession(state) {
    // This state may contain Tin's device bearer. LinkedIn cookies must never
    // be passed here; they are captured immediately before an HTTPS upload.
    await chrome.storage.local.set({ [SESSION_KEY]: state });
  }

  async function clearSession() {
    await chrome.storage.local.remove([SESSION_KEY, BROWSER_CONTEXT_KEY]);
    await Promise.all([
      chrome.alarms.clear(REFRESH_ALARM),
      chrome.alarms.clear(COOKIE_REFRESH_ALARM),
      chrome.alarms.clear(DISCONNECT_ALARM),
    ]);
  }

  function isLinkedInCookie(cookie) {
    const domain = String(cookie?.domain || "")
      .replace(/^\./, "")
      .toLowerCase();
    return (
      COOKIE_NAME_SET.has(cookie?.name) &&
      (domain === "linkedin.com" || domain.endsWith(".linkedin.com"))
    );
  }

  function serializeCookie(cookie) {
    const serialized = {
      name: cookie.name,
      value: String(cookie.value || ""),
      domain: String(cookie.domain || ""),
      path: String(cookie.path || "/"),
      secure: cookie.secure === true,
      http_only: cookie.httpOnly === true,
      same_site: typeof cookie.sameSite === "string" ? cookie.sameSite : "unspecified",
    };
    if (Number.isFinite(cookie.expirationDate)) {
      serialized.expiration_date = cookie.expirationDate;
    }
    return serialized;
  }

  async function captureLinkedInCookies() {
    const cookies = (await chrome.cookies.getAll({ domain: "linkedin.com" }))
      .filter(isLinkedInCookie)
      .map(serializeCookie)
      .sort((left, right) =>
        `${left.name}\u0000${left.domain}\u0000${left.path}`.localeCompare(
          `${right.name}\u0000${right.domain}\u0000${right.path}`,
        ),
      );
    const present = new Set(
      cookies.filter((cookie) => cookie.value).map((cookie) => cookie.name),
    );
    if (!present.has("li_at") || !present.has("JSESSIONID")) {
      throw new SessionError(
        "linkedin_signed_out",
        "Sign in to LinkedIn in this Chrome profile, then try again.",
      );
    }
    return cookies;
  }

  function base64Url(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function generateDeviceToken() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    return base64Url(bytes);
  }

  async function sha256Hex(value) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }

  async function sessionPayload(cookies, deviceTokenHash) {
    return {
      device_token_hash: deviceTokenHash,
      extension_version: manifestVersion(),
      user_agent: typeof navigator.userAgent === "string" ? navigator.userAgent : "",
      browser_context: await browserContext.capture(),
      cookies,
    };
  }

  async function parseJson(response) {
    if (response.status === 204) return {};
    try {
      const body = await response.json();
      return body && typeof body === "object" ? body : {};
    } catch (_error) {
      return {};
    }
  }

  function validationFailureFromResponse(responseBody) {
    const detail = responseBody?.detail;
    if (
      !detail ||
      typeof detail !== "object" ||
      Array.isArray(detail) ||
      detail.code !== VALIDATION_FAILURE_CODE
    ) {
      return null;
    }
    const reason = VALIDATION_REASONS.has(detail.reason) ? detail.reason : "unknown";
    const category = VALIDATION_CATEGORIES.has(detail.response_category)
      ? detail.response_category
      : "unknown";
    const responseKind = VALIDATION_RESPONSE_KINDS.has(detail.response_kind)
      ? detail.response_kind
      : "unknown";
    const upstreamStatus =
      Number.isInteger(detail.upstream_status) &&
      detail.upstream_status >= 100 &&
      detail.upstream_status <= 599
        ? detail.upstream_status
        : null;
    return {
      reason,
      category,
      responseKind,
      upstreamStatus,
      csrfDetected: detail.csrf_detected === true,
      challengeDetected: detail.challenge_detected === true,
    };
  }

  function validationSessionError(diagnostic, responseStatus) {
    const supportCode = [
      "LI",
      diagnostic.upstreamStatus || "NA",
      diagnostic.category.replaceAll("_", "-").toUpperCase(),
      diagnostic.responseKind.toUpperCase(),
    ].join("-");
    let code = "linkedin_validation_failed";
    let action =
      "LinkedIn rejected Tin's session check for this account. Refresh LinkedIn and try again.";

    if (
      diagnostic.challengeDetected ||
      diagnostic.reason === "checkpoint_required" ||
      diagnostic.category.startsWith("checkpoint_")
    ) {
      code = "linkedin_checkpoint_required";
      action =
        "Complete LinkedIn's verification in this Chrome profile, then try again.";
    } else if (diagnostic.csrfDetected || diagnostic.category === "csrf_rejected") {
      code = "linkedin_csrf_rejected";
      action = "Refresh your LinkedIn tab in this Chrome profile, then try again.";
    } else if (
      diagnostic.reason === "session_invalid" ||
      diagnostic.reason === "session_forbidden" ||
      diagnostic.category === "auth_redirect" ||
      diagnostic.category === "auth_rejected" ||
      diagnostic.category === "signed_out_response"
    ) {
      code = "linkedin_session_invalid";
      action = "Refresh or sign in to LinkedIn in this Chrome profile, then try again.";
    } else if (diagnostic.reason === "rate_limited") {
      code = "linkedin_rate_limited";
      action = "LinkedIn is rate-limiting the session check. Wait a few minutes, then retry.";
    } else if (diagnostic.reason === "request_rejected") {
      code = "linkedin_request_rejected";
    } else if (
      diagnostic.reason === "linkedin_unavailable" ||
      diagnostic.reason === "network_error" ||
      diagnostic.category === "upstream_failure"
    ) {
      code = "linkedin_validation_unavailable";
      action = "LinkedIn could not validate this session. Try again shortly.";
    } else if (
      diagnostic.reason === "unexpected_response" ||
      diagnostic.category === "unexpected_payload"
    ) {
      code = "linkedin_unexpected_response";
      action = "LinkedIn returned an unexpected session response. Refresh LinkedIn and retry.";
    }

    return new SessionError(
      code,
      `${action} If it still fails, send Tin support diagnostic ${supportCode}.`,
      responseStatus,
    );
  }

  async function apiRequest(apiBase, path, { method = "GET", bearer, body } = {}) {
    let response;
    try {
      const headers = new Headers({ Accept: "application/json" });
      if (bearer) headers.set("Authorization", `Bearer ${bearer}`);
      if (body !== undefined) headers.set("Content-Type", "application/json");
      response = await fetch(`${apiBase}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
      });
    } catch (_error) {
      throw new SessionError(
        "tin_unavailable",
        "Tin could not be reached. Check your connection and try again.",
      );
    }
    const responseBody = await parseJson(response);
    if (!response.ok) {
      const validationFailure = validationFailureFromResponse(responseBody);
      if (validationFailure) {
        throw validationSessionError(validationFailure, response.status);
      }
      const rotationRequired =
        response.status === 409 && responseBody?.detail === DEVICE_ROTATION_DETAIL;
      throw new SessionError(
        rotationRequired
          ? "device_rotation_required"
          : response.status === 401 || response.status === 403
            ? "session_unauthorized"
            : "tin_request_failed",
        rotationRequired
          ? "This browser credential must be replaced before LinkedIn can connect."
          : response.status === 401 || response.status === 403
            ? "Reconnect LinkedIn from Tin's Integrations panel."
            : "Tin could not update the LinkedIn session. Try again shortly.",
        response.status,
      );
    }
    return responseBody;
  }

  function actorFromResponse(body, fallback = null) {
    const candidate = body?.actor || body?.connection?.actor;
    if (!candidate || typeof candidate !== "object") return fallback;
    return {
      display_name:
        typeof candidate.display_name === "string"
          ? candidate.display_name.slice(0, 200)
          : null,
      profile_url:
        typeof candidate.profile_url === "string"
          ? candidate.profile_url.slice(0, 500)
          : null,
    };
  }

  function syncedAtFromResponse(body) {
    const value = body?.last_synced_at || body?.connection?.last_synced_at;
    return typeof value === "string" ? value.slice(0, 64) : new Date().toISOString();
  }

  function remoteSessionState(body) {
    const identityStatus =
      typeof body?.device?.identity_status === "string"
        ? body.device.identity_status.trim().toLowerCase()
        : "unknown";
    const statusReason =
      typeof body?.device?.status_reason === "string" &&
      body.device.status_reason.trim()
        ? body.device.status_reason.trim().slice(0, 128)
        : null;

    // Treat the control plane as authoritative and fail closed. A device can
    // still authenticate while its LinkedIn session is checkpointed, expired,
    // or otherwise unusable by the cloud executor.
    if (body?.ready === true && identityStatus === "active") {
      return { status: "connected", statusCode: "session_synced" };
    }
    if (identityStatus === "signed_out") {
      return {
        status: "signed_out",
        statusCode: statusReason || "linkedin_signed_out",
      };
    }
    if (identityStatus === "checkpoint") {
      return {
        status: "checkpoint",
        statusCode: statusReason || "linkedin_checkpoint",
      };
    }
    if (identityStatus === "error") {
      return {
        status: "error",
        statusCode: statusReason || "remote_session_error",
      };
    }
    return {
      status: "needs_reconnect",
      statusCode:
        statusReason ||
        (identityStatus === "refresh_required"
          ? "linkedin_refresh_required"
          : identityStatus === "mismatch"
            ? "linkedin_identity_mismatch"
            : "remote_session_not_ready"),
    };
  }

  async function scheduleRefreshes() {
    await chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: REFRESH_MINUTES });
  }

  async function signedOutStatus(existing = null) {
    const state = {
      ...(existing || {}),
      status: "signed_out",
      status_code: "linkedin_signed_out",
    };
    if (existing?.device_token) await saveSession(state);
    return publicStatus(state);
  }

  function connectingState(payload, deviceToken) {
    return {
      status: "connecting",
      status_code: "session_connecting",
      api_base: payload.api_base,
      project_id: payload.project_id,
      device_token: deviceToken,
      actor: null,
      last_synced_at: null,
    };
  }

  async function retireDeviceCredential(state) {
    if (state?.device_token && state?.api_base) {
      try {
        await apiRequest(state.api_base, "/api/integrations/linkedin/session/device", {
          method: "DELETE",
          bearer: state.device_token,
        });
      } catch (error) {
        // An already-rejected bearer is retired by definition. Every other
        // failure must leave the local credential in place so it is never
        // silently orphaned server-side.
        if (error?.status !== 401) {
          throw new SessionError(
            "device_rotation_failed",
            "Tin could not safely replace this browser credential. Try again shortly.",
            error?.status || null,
          );
        }
      }
    }
    await Promise.all([
      chrome.alarms.clear(REFRESH_ALARM),
      chrome.alarms.clear(COOKIE_REFRESH_ALARM),
      chrome.alarms.clear(DISCONNECT_ALARM),
    ]);
  }

  // Worker-only migration hook. Retire this browser device through the existing
  // actor fence; do not disconnect projects or touch the LinkedIn login.
  globalThis.TinLinkedInRetireLegacy = () => serializeSessionOperation(async () => {
    const state = await loadSession();
    await retireDeviceCredential(state);
    await clearSession();
  });

  async function uploadConnectedSession(payload, cookies, deviceToken) {
    return apiRequest(payload.api_base, "/api/integrations/linkedin/session/connect", {
      method: "POST",
      bearer: payload.session_grant,
      body: {
        project_id: payload.project_id,
        ...(await sessionPayload(cookies, await sha256Hex(deviceToken))),
      },
    });
  }

  async function connectSession(payload) {
    let cookies;
    try {
      cookies = await captureLinkedInCookies();
    } catch (error) {
      if (error?.code === "linkedin_signed_out") return signedOutStatus();
      throw error;
    }
    const existing = await loadSession();
    const canReuse = Boolean(
      existing?.device_token &&
        existing?.api_base === payload.api_base &&
        existing?.status !== "disconnect_pending",
    );
    let deviceToken;
    if (canReuse) {
      // The bearer represents this Chrome installation for the Tin user, not
      // one project. Reuse it while the signed grant adds an independent
      // project delegation on the control plane.
      deviceToken = existing.device_token;
    } else {
      // A pending disconnect or environment switch must finish revocation
      // before local state is overwritten with a replacement credential.
      await retireDeviceCredential(existing);
      deviceToken = generateDeviceToken();
      // Persist the opaque candidate before upload. If the request commits but
      // its response is lost, the next connect/refresh can recover the exact
      // server-side bearer instead of creating an orphan.
      await saveSession(connectingState(payload, deviceToken));
    }

    let body;
    try {
      body = await uploadConnectedSession(payload, cookies, deviceToken);
    } catch (error) {
      if (error?.code !== "device_rotation_required") throw error;

      // A revoked credential must never be resurrected, and a credential from
      // another Tin login must never be rebound. Possession authorizes only
      // revoking that old browser device; project delegations remain intact.
      await retireDeviceCredential({
        api_base: payload.api_base,
        device_token: deviceToken,
      });
      deviceToken = generateDeviceToken();
      await saveSession(connectingState(payload, deviceToken));
      try {
        body = await uploadConnectedSession(payload, cookies, deviceToken);
      } catch (retryError) {
        if (retryError?.code === "device_rotation_required") {
          throw new SessionError(
            "device_rotation_failed",
            "Tin could not safely replace this browser credential. Try again shortly.",
            retryError?.status || null,
          );
        }
        throw retryError;
      }
    }
    const state = {
      status: "connected",
      status_code: "session_synced",
      api_base: payload.api_base,
      project_id: payload.project_id,
      device_token: deviceToken,
      actor: actorFromResponse(body),
      last_synced_at: syncedAtFromResponse(body),
    };
    await saveSession(state);
    await chrome.alarms.clear(DISCONNECT_ALARM);
    await scheduleRefreshes();
    return publicStatus(state);
  }

  async function refreshSession({ quiet = false } = {}) {
    const state = await loadSession();
    if (!state?.device_token || !state?.api_base || !state?.project_id) {
      return publicStatus(state);
    }
    if (state.status === "disconnect_pending") return disconnectSession({ retry: true });
    let cookies;
    try {
      cookies = await captureLinkedInCookies();
    } catch (error) {
      if (error?.code === "linkedin_signed_out") return signedOutStatus(state);
      throw error;
    }
    try {
      const body = await apiRequest(
        state.api_base,
        "/api/integrations/linkedin/session/refresh",
        {
          method: "POST",
          bearer: state.device_token,
          body: await sessionPayload(cookies, await sha256Hex(state.device_token)),
        },
      );
      const refreshed = {
        ...state,
        status: "connected",
        status_code: "session_synced",
        actor: actorFromResponse(body, state.actor),
        last_synced_at: syncedAtFromResponse(body),
      };
      await saveSession(refreshed);
      await scheduleRefreshes();
      return publicStatus(refreshed);
    } catch (error) {
      if (error?.code === "session_unauthorized") {
        const reconnect = {
          status: "needs_reconnect",
          status_code: "device_credential_rejected",
          api_base: state.api_base,
          project_id: state.project_id,
          actor: state.actor,
          last_synced_at: state.last_synced_at,
        };
        await saveSession(reconnect);
        await chrome.alarms.clear(REFRESH_ALARM);
        return publicStatus(reconnect);
      }
      if (quiet) {
        const unavailable = {
          ...state,
          status: "error",
          status_code: error?.code || "refresh_failed",
        };
        await saveSession(unavailable);
        return publicStatus(unavailable);
      }
      throw error;
    }
  }

  async function remoteStatus() {
    const state = await loadSession();
    if (!state?.device_token || !state?.api_base) return publicStatus(state);
    if (state.status === "disconnect_pending") return publicStatus(state);
    try {
      const body = await apiRequest(
        state.api_base,
        "/api/integrations/linkedin/session/device",
        { bearer: state.device_token },
      );
      const remote = remoteSessionState(body);
      const next = {
        ...state,
        status: remote.status,
        status_code: remote.statusCode,
        actor: actorFromResponse(body, state.actor),
        last_synced_at:
          typeof body?.last_synced_at === "string"
            ? body.last_synced_at.slice(0, 64)
            : state.last_synced_at,
      };
      await saveSession(next);
      return publicStatus(next);
    } catch (error) {
      if (error?.code === "session_unauthorized") {
        const reconnect = {
          status: "needs_reconnect",
          status_code: "device_credential_rejected",
          api_base: state.api_base,
          project_id: state.project_id,
          actor: state.actor,
          last_synced_at: state.last_synced_at,
        };
        await saveSession(reconnect);
        return publicStatus(reconnect);
      }
      return publicStatus({
        ...state,
        status: "error",
        status_code: error?.code || "status_failed",
      });
    }
  }

  async function disconnectSession({ retry = false } = {}) {
    const state = await loadSession();
    if (!state?.device_token || !state?.api_base) {
      await clearSession();
      return publicStatus(null);
    }
    try {
      await apiRequest(state.api_base, "/api/integrations/linkedin/session/device", {
        method: "DELETE",
        bearer: state.device_token,
      });
      await clearSession();
      return publicStatus(null);
    } catch (error) {
      if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        await clearSession();
        return publicStatus(null);
      }
      const pending = {
        ...state,
        status: "disconnect_pending",
        status_code: "disconnect_retry_scheduled",
      };
      await saveSession(pending);
      await chrome.alarms.create(DISCONNECT_ALARM, {
        delayInMinutes: retry ? DISCONNECT_RETRY_MINUTES : 1,
      });
      return publicStatus(pending);
    }
  }

  async function handleBridge(message, sender) {
    let senderOrigin = null;
    try {
      senderOrigin = new URL(sender?.tab?.url || "").origin;
    } catch (_error) {
      senderOrigin = null;
    }
    if (!senderOrigin || !Protocol.isAllowedDashboardOrigin(senderOrigin)) {
      throw new SessionError("invalid_sender", "This request did not come from Tin.");
    }
    const request = Protocol.validateRequest({
      source: Protocol.DASHBOARD_SOURCE,
      protocol_version: message.protocol_version,
      type: message.type,
      request_id: message.request_id,
      payload: message.payload,
    });
    if (request.type === "CONNECT_SESSION") {
      return serializeSessionOperation(() => connectSession(request.payload));
    }
    if (request.type === "REFRESH") {
      return serializeSessionOperation(() => refreshSession());
    }
    if (request.type === "DISCONNECT") {
      return serializeSessionOperation(() => disconnectSession());
    }
    return serializeSessionOperation(() => remoteStatus());
  }

  async function handlePopup(message, sender) {
    if (sender?.id && sender.id !== chrome.runtime.id) {
      throw new SessionError("invalid_sender", "This request did not come from Tin.");
    }
    if (message.type === "REFRESH") {
      return serializeSessionOperation(() => refreshSession());
    }
    if (message.type === "DISCONNECT") {
      return serializeSessionOperation(() => disconnectSession());
    }
    if (message.type === "STATUS") {
      return serializeSessionOperation(() => remoteStatus());
    }
    throw new SessionError("invalid_request", "Unsupported popup request.");
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const operation =
      message?.namespace === Protocol.EXTENSION_SOURCE &&
      message?.protocol_version === Protocol.VERSION
        ? handleBridge(message, sender)
        : message?.namespace === POPUP_NAMESPACE
          ? handlePopup(message, sender)
          : null;
    if (!operation) return false;
    Promise.resolve(operation)
      .then((payload) => sendResponse({ ok: true, payload: publicStatus(payload) }))
      .catch((error) =>
        sendResponse({
          ok: false,
          error: {
            code: typeof error?.code === "string" ? error.code : "extension_error",
            message:
              typeof error?.message === "string"
                ? error.message.slice(0, 300)
                : "The extension could not complete that request.",
          },
        }),
      );
    return true;
  });

  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === REFRESH_ALARM || alarm.name === COOKIE_REFRESH_ALARM) {
      void serializeSessionOperation(() => refreshSession({ quiet: true }));
    } else if (alarm.name === DISCONNECT_ALARM) {
      void serializeSessionOperation(() => disconnectSession({ retry: true }));
    }
  });

  chrome.cookies.onChanged.addListener((changeInfo) => {
    if (!isLinkedInCookie(changeInfo?.cookie)) return;
    void loadSession().then((state) => {
      if (!state?.device_token || state.status === "disconnect_pending") return;
      return chrome.alarms.create(COOKIE_REFRESH_ALARM, {
        delayInMinutes: COOKIE_REFRESH_DELAY_MINUTES,
      });
    });
  });

  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => browserContext.observe(details),
    { urls: ["https://www.linkedin.com/voyager/api/*"] },
    ["requestHeaders", "extraHeaders"],
  );

  async function initialize() {
    try {
      await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    } catch (_error) {
      // Chrome versions supporting this manifest normally provide the API. A
      // missing access-level method must not stop session refresh entirely.
    }
    const state = await loadSession();
    if (state?.device_token && state.status !== "disconnect_pending") {
      await scheduleRefreshes();
    } else if (state?.status === "disconnect_pending") {
      await chrome.alarms.create(DISCONNECT_ALARM, { delayInMinutes: 1 });
    }
  }

  void serializeSessionOperation(() => initialize());
})();

importScripts("collection/worker.js");
