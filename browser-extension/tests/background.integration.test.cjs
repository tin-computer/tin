"use strict";

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const test = require("node:test");

const {
  createBackgroundHarness,
  defaultBrowserContext,
  defaultCookies,
  eventually,
  settle,
} = require("./helpers/extension-script-harness.cjs");

const SESSION_KEY = "tin.linkedin.session.v2";
const REFRESH_ALARM = "tin.linkedin.session-refresh.v2";
const COOKIE_REFRESH_ALARM = "tin.linkedin.cookie-refresh.v2";
const DISCONNECT_ALARM = "tin.linkedin.disconnect-retry.v2";

function connectedSession(overrides = {}) {
  return {
    status: "connected",
    status_code: "session_synced",
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    device_token: "raw-device-token-kept-inside-extension",
    actor: {
      display_name: "Founder Contract",
      profile_url: "https://www.linkedin.com/in/founder-contract",
    },
    last_synced_at: "2026-08-06T17:00:00Z",
    ...overrides,
  };
}

test("CONNECT_SESSION uploads only allowlisted LinkedIn cookies from the worker", async () => {
  const harness = createBackgroundHarness({
    fetchHandler(call) {
      assert.equal(
        new URL(call.url).pathname,
        "/api/integrations/linkedin/session/connect",
      );
      return {
        body: {
          actor: {
            display_name: "Founder Contract",
            profile_url: "https://www.linkedin.com/in/founder-contract",
          },
          last_synced_at: "2026-08-06T17:01:00Z",
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, true);
  assert.deepEqual(response.payload, {
    status: "connected",
    connected: true,
    project_id: "project_contract",
    actor: {
      display_name: "Founder Contract",
      profile_url: "https://www.linkedin.com/in/founder-contract",
    },
    last_synced_at: "2026-08-06T17:01:00Z",
    status_code: "session_synced",
    extension_version: "0.2.0-test",
  });

  const call = harness.fetchCalls[0];
  assert.equal(call.method, "POST");
  assert.equal(call.authorization, "Bearer one-time-dashboard-grant");
  assert.equal(call.credentials, "omit");
  assert.equal(call.redirect, "error");
  assert.equal(call.body.project_id, "project_contract");
  assert.match(call.body.device_token_hash, /^[a-f0-9]{64}$/);
  assert.equal(call.body.extension_version, "0.2.0-test");
  assert.equal(call.body.user_agent, "Tin extension integration test");
  assert.deepEqual(call.body.browser_context, defaultBrowserContext());
  assert.deepEqual(
    call.body.cookies.map((cookie) => cookie.name),
    ["JSESSIONID", "li_at"],
  );
  assert.equal(JSON.stringify(call.body).includes("must-never-upload"), false);

  const stored = harness.storageValue(SESSION_KEY);
  assert.match(stored.device_token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(Object.hasOwn(stored, "cookies"), false);
  assert.equal(JSON.stringify(stored).includes("linkedin-session-secret"), false);
  assert.equal(JSON.stringify(stored).includes("one-time-dashboard-grant"), false);
  assert.equal(JSON.stringify(response).includes(stored.device_token), false);
  assert.equal(JSON.stringify(response).includes("one-time-dashboard-grant"), false);
  assert.equal(harness.alarms.has(REFRESH_ALARM), true);
});

test("CONNECT_SESSION observes only allowlisted browser context from LinkedIn traffic", async () => {
  const harness = createBackgroundHarness({
    browserContext: null,
    fetchHandler() {
      return {
        body: {
          actor: {
            display_name: "Olcay",
            profile_url: "https://www.linkedin.com/in/olcay",
          },
          last_synced_at: "2026-08-10T22:00:00Z",
        },
      };
    },
  });
  await settle();

  const connecting = harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });
  await eventually(() => harness.tabCreates.length === 1);
  await harness.emitVoyagerRequestHeaders([
    { name: "Accept-Language", value: "en-US,en;q=0.9" },
    { name: "Cookie", value: "li_at=must-never-enter-browser-context" },
    { name: "Csrf-Token", value: "ajax:must-never-enter-browser-context" },
    { name: "X-Li-Lang", value: "en_US" },
    { name: "X-Li-Page-Instance", value: "urn:li:page:must-not-be-captured" },
    { name: "X-Li-Track", value: JSON.stringify(defaultBrowserContext().li_track) },
  ]);

  const response = await connecting;
  assert.equal(response.ok, true);
  const captured = harness.fetchCalls[0].body.browser_context;
  assert.equal(JSON.stringify(captured).includes("must-never"), false);
  assert.deepEqual(Object.keys(captured).sort(), [
    "accept_language",
    "li_lang",
    "li_track",
  ]);
});

test("control-plane errors never reflect cookie or grant values", async () => {
  const harness = createBackgroundHarness({
    fetchHandler() {
      return {
        status: 422,
        body: {
          detail:
            "li_at=linkedin-session-secret Bearer one-time-dashboard-grant",
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, false);
  assert.equal(response.error.code, "tin_request_failed");
  assert.equal(JSON.stringify(response).includes("linkedin-session-secret"), false);
  assert.equal(JSON.stringify(response).includes("one-time-dashboard-grant"), false);
  const pending = harness.storageValue(SESSION_KEY);
  assert.equal(pending.status, "connecting");
  assert.match(pending.device_token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(Object.hasOwn(pending, "cookies"), false);
  assert.equal(JSON.stringify(pending).includes("linkedin-session-secret"), false);
  assert.equal(JSON.stringify(pending).includes("one-time-dashboard-grant"), false);
});

test("safe LinkedIn validation diagnostics become actionable extension errors", async () => {
  const harness = createBackgroundHarness({
    fetchHandler() {
      return {
        status: 502,
        body: {
          detail: {
            code: "linkedin_session_validation_failed",
            reason: "request_rejected",
            upstream_status: 400,
            response_category: "client_rejected",
            response_kind: "json",
            csrf_detected: false,
            challenge_detected: false,
            reflected_cookie: "linkedin-session-secret",
          },
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, false);
  assert.equal(response.error.code, "linkedin_request_rejected");
  assert.equal(
    response.error.message,
    "LinkedIn rejected Tin's session check for this account. Refresh LinkedIn and try again. If it still fails, send Tin support diagnostic LI-400-CLIENT-REJECTED-JSON.",
  );
  assert.equal(JSON.stringify(response).includes("linkedin-session-secret"), false);
  assert.equal(JSON.stringify(response).includes("one-time-dashboard-grant"), false);
});

test("CSRF diagnostics give the account owner a specific recovery step", async () => {
  const harness = createBackgroundHarness({
    fetchHandler() {
      return {
        status: 401,
        body: {
          detail: {
            code: "linkedin_session_validation_failed",
            reason: "session_invalid",
            upstream_status: 403,
            response_category: "csrf_rejected",
            response_kind: "json",
            csrf_detected: true,
            challenge_detected: false,
          },
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, false);
  assert.equal(response.error.code, "linkedin_csrf_rejected");
  assert.match(response.error.message, /Refresh your LinkedIn tab/);
  assert.match(response.error.message, /LI-403-CSRF-REJECTED-JSON/);
});

test("untrusted validation fields cannot cross the extension bridge", async () => {
  const harness = createBackgroundHarness({
    fetchHandler() {
      return {
        status: 502,
        body: {
          detail: {
            code: "linkedin_session_validation_failed",
            reason: "li_at=linkedin-session-secret",
            upstream_status: "400 private-session-cookie",
            response_category: "Bearer one-time-dashboard-grant",
            response_kind: "<script>private-session-cookie</script>",
            csrf_detected: "private-session-cookie",
            challenge_detected: "private-session-cookie",
          },
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, false);
  assert.equal(response.error.code, "linkedin_validation_failed");
  assert.match(response.error.message, /LI-NA-UNKNOWN-UNKNOWN/);
  assert.equal(JSON.stringify(response).includes("linkedin-session-secret"), false);
  assert.equal(JSON.stringify(response).includes("private-session-cookie"), false);
  assert.equal(JSON.stringify(response).includes("one-time-dashboard-grant"), false);
  assert.equal(JSON.stringify(response).includes("<script>"), false);
});

test("connecting another project reuses the user-global browser credential", async () => {
  const existing = connectedSession();
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: existing },
    fetchHandler(call) {
      assert.equal(call.method, "POST");
      assert.equal(
        new URL(call.url).pathname,
        "/api/integrations/linkedin/session/connect",
      );
      return {
        body: {
          actor: existing.actor,
          last_synced_at: "2026-08-06T17:02:00Z",
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: existing.api_base,
    project_id: "project_second",
    session_grant: "second-project-consent-grant",
  });

  assert.equal(response.ok, true);
  assert.equal(harness.fetchCalls.length, 1);
  assert.equal(harness.fetchCalls[0].method, "POST");
  assert.equal(
    harness.fetchCalls[0].body.device_token_hash,
    createHash("sha256").update(existing.device_token).digest("hex"),
  );
  assert.equal(harness.storageValue(SESSION_KEY).device_token, existing.device_token);
  assert.equal(harness.storageValue(SESSION_KEY).project_id, "project_second");
});

test("concurrent project connects serialize onto one browser credential", async () => {
  let releaseFirst;
  const firstResponse = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const harness = createBackgroundHarness({
    async fetchHandler(call, index) {
      assert.equal(call.method, "POST");
      if (index === 0) await firstResponse;
      return {
        body: {
          actor: { display_name: "Founder Contract" },
          last_synced_at: `2026-08-06T17:0${index + 2}:00Z`,
        },
      };
    },
  });
  await settle();

  const first = harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_first",
    session_grant: "first-consent-grant",
  });
  await eventually(() => harness.fetchCalls.length === 1);
  const second = harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_second",
    session_grant: "second-consent-grant",
  });
  await settle();

  assert.equal(harness.fetchCalls.length, 1);
  releaseFirst();
  const [firstResult, secondResult] = await Promise.all([first, second]);

  assert.equal(firstResult.ok, true);
  assert.equal(secondResult.ok, true);
  assert.equal(harness.fetchCalls.length, 2);
  assert.equal(
    harness.fetchCalls[0].body.device_token_hash,
    harness.fetchCalls[1].body.device_token_hash,
  );
  assert.equal(harness.storageValue(SESSION_KEY).project_id, "project_second");
});

for (const retiredStatus of [204, 401]) {
  test(`a device rotation response retires status ${retiredStatus} and retries with a fresh bearer`, async () => {
    const existing = connectedSession();
    const harness = createBackgroundHarness({
      initialStorage: { [SESSION_KEY]: existing },
      fetchHandler(call, index) {
        if (index === 0) {
          assert.equal(call.method, "POST");
          return {
            status: 409,
            body: { detail: "LinkedIn device credential must rotate" },
          };
        }
        if (index === 1) {
          assert.equal(call.method, "DELETE");
          assert.equal(call.authorization, `Bearer ${existing.device_token}`);
          return { status: retiredStatus };
        }
        assert.equal(index, 2);
        assert.equal(call.method, "POST");
        return {
          body: {
            actor: existing.actor,
            last_synced_at: "2026-08-06T17:03:00Z",
          },
        };
      },
    });
    await settle();

    const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
      api_base: existing.api_base,
      project_id: "project_rotated",
      session_grant: "fresh-consent-grant",
    });

    assert.equal(response.ok, true);
    assert.deepEqual(
      harness.fetchCalls.map((call) => call.method),
      ["POST", "DELETE", "POST"],
    );
    assert.notEqual(
      harness.fetchCalls[0].body.device_token_hash,
      harness.fetchCalls[2].body.device_token_hash,
    );
    assert.notEqual(harness.storageValue(SESSION_KEY).device_token, existing.device_token);
    assert.equal(JSON.stringify(response).includes(existing.device_token), false);
  });
}

for (const cleanupStatus of [403, 404, 503]) {
  test(`rotation fails closed when old-bearer cleanup returns ${cleanupStatus}`, async () => {
    const existing = connectedSession();
    const harness = createBackgroundHarness({
      initialStorage: { [SESSION_KEY]: existing },
      fetchHandler(call, index) {
        if (index === 0) {
          return {
            status: 409,
            body: { detail: "LinkedIn device credential must rotate" },
          };
        }
        assert.equal(call.method, "DELETE");
        return { status: cleanupStatus };
      },
    });
    await settle();

    const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
      api_base: existing.api_base,
      project_id: "project_other_user",
      session_grant: "other-user-consent-grant",
    });

    assert.equal(response.ok, false);
    assert.equal(response.error.code, "device_rotation_failed");
    assert.equal(harness.fetchCalls.length, 2);
    assert.deepEqual(harness.storageValue(SESSION_KEY), existing);
  });
}

test("switching Tin environments revokes before replacing local state", async () => {
  const existing = connectedSession({ api_base: "https://api.staging.tin.computer" });
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: existing },
    fetchHandler(call, index) {
      if (index === 0) {
        assert.equal(call.method, "DELETE");
        assert.equal(call.url, `${existing.api_base}/api/integrations/linkedin/session/device`);
        return { status: 204 };
      }
      assert.equal(call.method, "POST");
      assert.equal(
        call.url,
        "https://api.tin.computer/api/integrations/linkedin/session/connect",
      );
      return {
        body: {
          actor: existing.actor,
          last_synced_at: "2026-08-06T17:04:00Z",
        },
      };
    },
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_production",
    session_grant: "production-consent-grant",
  });

  assert.equal(response.ok, true);
  assert.deepEqual(
    harness.fetchCalls.map((call) => call.method),
    ["DELETE", "POST"],
  );
  assert.notEqual(harness.storageValue(SESSION_KEY).device_token, existing.device_token);
  assert.equal(harness.storageValue(SESSION_KEY).api_base, "https://api.tin.computer");
});

test("CONNECT_SESSION reports signed out without generating or uploading credentials", async () => {
  const harness = createBackgroundHarness({
    cookies: defaultCookies().filter((cookie) => cookie.name !== "li_at"),
  });
  await settle();

  const response = await harness.deliverBridgeMessage("CONNECT_SESSION", {
    api_base: "https://api.tin.computer",
    project_id: "project_contract",
    session_grant: "one-time-dashboard-grant",
  });

  assert.equal(response.ok, true);
  assert.equal(response.payload.status, "signed_out");
  assert.equal(response.payload.connected, false);
  assert.equal(harness.fetchCalls.length, 0);
  assert.equal(harness.hasStorage(SESSION_KEY), false);
});

test("periodic refresh re-reads cookies and authenticates with the local device token", async () => {
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: connectedSession() },
    fetchHandler(call) {
      assert.equal(
        new URL(call.url).pathname,
        "/api/integrations/linkedin/session/refresh",
      );
      return { body: { last_synced_at: "2026-08-06T17:30:00Z" } };
    },
  });
  await settle();
  assert.equal(harness.alarms.has(REFRESH_ALARM), true);

  await harness.fireAlarm(REFRESH_ALARM);
  await eventually(() => harness.fetchCalls.length === 1);

  assert.equal(harness.fetchCalls.length, 1);
  assert.equal(
    harness.fetchCalls[0].authorization,
    "Bearer raw-device-token-kept-inside-extension",
  );
  assert.match(harness.fetchCalls[0].body.device_token_hash, /^[a-f0-9]{64}$/);
  assert.equal(harness.storageValue(SESSION_KEY).last_synced_at, "2026-08-06T17:30:00Z");
  assert.equal(Object.hasOwn(harness.storageValue(SESSION_KEY), "cookies"), false);
});

test("a relevant cookie change schedules a refresh without exposing the cookie", async () => {
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: connectedSession() },
  });
  await settle();

  await harness.emitCookieChange({
    name: "li_at",
    value: "new-secret-value",
    domain: ".linkedin.com",
    path: "/",
  });
  assert.equal(harness.alarms.has(COOKIE_REFRESH_ALARM), true);
  assert.equal(
    JSON.stringify(harness.storageValue(SESSION_KEY)).includes("new-secret-value"),
    false,
  );

  const createsBefore = harness.alarmCreates.length;
  await harness.emitCookieChange({
    name: "tracking_cookie",
    value: "irrelevant",
    domain: ".linkedin.com",
    path: "/",
  });
  assert.equal(harness.alarmCreates.length, createsBefore);
});

test("STATUS and DISCONNECT use the device bearer and expose only sanitized state", async () => {
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: connectedSession() },
    fetchHandler(call) {
      const pathname = new URL(call.url).pathname;
      if (call.method === "GET" && pathname.endsWith("/session/device")) {
        return {
          body: {
            ready: true,
            actor: { display_name: "Founder Contract" },
            device: { identity_status: "active", status_reason: "" },
            last_synced_at: "2026-08-06T17:40:00Z",
            cookie: "must-not-cross",
          },
        };
      }
      if (call.method === "DELETE" && pathname.endsWith("/session/device")) {
        return { status: 204 };
      }
      throw new Error(`Unexpected request: ${call.method} ${pathname}`);
    },
  });
  await settle();

  const status = await harness.deliverBridgeMessage("STATUS");
  assert.equal(status.ok, true);
  assert.equal(status.payload.status, "connected");
  assert.equal(status.payload.actor.display_name, "Founder Contract");
  assert.equal(JSON.stringify(status).includes("raw-device-token"), false);
  assert.equal(JSON.stringify(status).includes("must-not-cross"), false);

  const disconnected = await harness.deliverBridgeMessage("DISCONNECT");
  assert.equal(disconnected.ok, true);
  assert.equal(disconnected.payload.status, "not_connected");
  assert.equal(harness.hasStorage(SESSION_KEY), false);
  assert.equal(harness.fetchCalls[1].method, "DELETE");
  assert.equal(
    harness.fetchCalls[1].authorization,
    "Bearer raw-device-token-kept-inside-extension",
  );
});

for (const scenario of [
  {
    name: "checkpoint",
    ready: false,
    identityStatus: "checkpoint",
    statusReason: "linkedin_checkpoint",
    expectedStatus: "checkpoint",
    expectedCode: "linkedin_checkpoint",
  },
  {
    name: "refresh required",
    ready: false,
    identityStatus: "refresh_required",
    statusReason: "linkedin_session_expired",
    expectedStatus: "needs_reconnect",
    expectedCode: "linkedin_session_expired",
  },
  {
    name: "not ready despite an active identity",
    ready: false,
    identityStatus: "active",
    statusReason: "",
    expectedStatus: "needs_reconnect",
    expectedCode: "remote_session_not_ready",
  },
]) {
  test(`STATUS fails closed when the remote session is ${scenario.name}`, async () => {
    const harness = createBackgroundHarness({
      initialStorage: { [SESSION_KEY]: connectedSession() },
      fetchHandler(call) {
        assert.equal(call.method, "GET");
        assert.equal(
          new URL(call.url).pathname,
          "/api/integrations/linkedin/session/device",
        );
        return {
          body: {
            ready: scenario.ready,
            actor: { display_name: "Founder Contract" },
            device: {
              identity_status: scenario.identityStatus,
              status_reason: scenario.statusReason,
            },
            last_synced_at: "2026-08-06T17:45:00Z",
          },
        };
      },
    });
    await settle();

    const response = await harness.deliverBridgeMessage("STATUS");

    assert.equal(response.ok, true);
    assert.equal(response.payload.status, scenario.expectedStatus);
    assert.equal(response.payload.connected, false);
    assert.equal(response.payload.status_code, scenario.expectedCode);
    assert.equal(harness.storageValue(SESSION_KEY).status, scenario.expectedStatus);
    assert.equal(
      harness.storageValue(SESSION_KEY).status_code,
      scenario.expectedCode,
    );
  });
}

test("failed disconnect retains the bearer only for background retry", async () => {
  let attempts = 0;
  const harness = createBackgroundHarness({
    initialStorage: { [SESSION_KEY]: connectedSession() },
    fetchHandler() {
      attempts += 1;
      return attempts === 1 ? { status: 503 } : { status: 204 };
    },
  });
  await settle();

  const response = await harness.deliverPopupMessage("DISCONNECT");
  assert.equal(response.ok, true);
  assert.equal(response.payload.status, "disconnect_pending");
  assert.equal(harness.storageValue(SESSION_KEY).device_token, connectedSession().device_token);
  assert.equal(harness.alarms.has(DISCONNECT_ALARM), true);
  assert.equal(JSON.stringify(response).includes(connectedSession().device_token), false);

  await harness.fireAlarm(DISCONNECT_ALARM);
  assert.equal(attempts, 2);
  assert.equal(harness.hasStorage(SESSION_KEY), false);
});

test("bridge calls from non-Tin pages are rejected before cookie access", async () => {
  const harness = createBackgroundHarness();
  await settle();
  const response = await harness.deliverBridgeMessage(
    "CONNECT_SESSION",
    {
      api_base: "https://api.tin.computer",
      project_id: "project_contract",
      session_grant: "grant",
    },
    "https://tin.computer.evil.test/home",
  );
  assert.equal(response.ok, false);
  assert.equal(response.error.code, "invalid_sender");
  assert.equal(harness.cookieQueries.length, 0);
});


test("Tin Lite migration retires the old device before clearing local authority", async () => {
  const harness=createBackgroundHarness({ initialStorage:{[SESSION_KEY]:connectedSession()},
    fetchHandler(call) { assert.equal(call.method,"DELETE");assert.equal(new URL(call.url).pathname,"/api/integrations/linkedin/session/device");return {status:204}; } });
  await settle();await harness.retireLegacyForCollection();
  assert.equal(harness.hasStorage(SESSION_KEY),false);
  assert.equal(harness.fetchCalls.length,1);
});

test("uncertain legacy retirement preserves the credential for reconciliation", async () => {
  const harness=createBackgroundHarness({ initialStorage:{[SESSION_KEY]:connectedSession()},
    fetchHandler() {return {status:503,body:{detail:"fixture unavailable"}};} });
  await settle();await assert.rejects(()=>harness.retireLegacyForCollection());
  assert.equal(harness.storageValue(SESSION_KEY).device_token,connectedSession().device_token);
});
