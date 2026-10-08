"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { createBridgeHarness } = require("./helpers/extension-script-harness.cjs");

const DASHBOARD_SOURCE = "tin.dashboard.linkedin-session.v2";

function request(type, payload = {}) {
  return {
    source: DASHBOARD_SOURCE,
    protocol_version: 2,
    type,
    request_id: `bridge-${type.toLowerCase()}`,
    payload,
  };
}

test("page bridge relays CONNECT_SESSION and strips cookies and device credentials", async () => {
  const harness = createBridgeHarness({
    backgroundResponse() {
      return {
        ok: true,
        payload: {
          status: "connected",
          project_id: "project_contract",
          actor: {
            display_name: "Founder Contract",
            profile_url: "https://www.linkedin.com/in/founder-contract",
          },
          last_synced_at: "2026-08-06T17:00:00Z",
          status_code: "session_synced",
          extension_version: "0.2.0-test",
          device_token: "tin-device-must-not-cross",
          cookies: [{ name: "li_at", value: "cookie-must-not-cross" }],
        },
      };
    },
  });

  assert.equal(harness.messageListenerCount, 1);
  assert.equal(harness.postedMessages[0].value.type, "READY");
  await harness.emitDashboardMessage(
    request("CONNECT_SESSION", {
      api_base: "https://api.tin.computer",
      project_id: "project_contract",
      session_grant: "one-time-session-grant",
    }),
  );

  assert.deepEqual(harness.forwardedMessages[0], {
    namespace: "tin.linkedin.session.v2",
    protocol_version: 2,
    type: "CONNECT_SESSION",
    request_id: "bridge-connect_session",
    payload: {
      api_base: "https://api.tin.computer",
      project_id: "project_contract",
      session_grant: "one-time-session-grant",
    },
  });
  const result = harness.postedMessages.at(-1).value;
  assert.equal(result.ok, true);
  assert.equal(result.payload.status, "connected");
  assert.equal(result.payload.actor.display_name, "Founder Contract");
  assert.doesNotMatch(JSON.stringify(result), /must-not-cross/);
});

test("page bridge rejects attempts to inject cookie data", async () => {
  const harness = createBridgeHarness();
  await harness.emitDashboardMessage(
    request("CONNECT_SESSION", {
      api_base: "https://api.tin.computer",
      project_id: "project_contract",
      session_grant: "one-time-session-grant",
      cookies: [{ name: "li_at", value: "secret" }],
    }),
  );
  assert.equal(harness.forwardedMessages.length, 0);
  const result = harness.postedMessages.at(-1).value;
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "invalid_request");
});

test("page bridge installs no listener outside Tin's exact origins", () => {
  const harness = createBridgeHarness({ origin: "https://tin.computer.evil.test" });
  assert.equal(harness.messageListenerCount, 0);
  assert.equal(harness.postedMessages.length, 0);
  assert.equal(harness.forwardedMessages.length, 0);
});
