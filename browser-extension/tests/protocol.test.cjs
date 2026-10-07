"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const Protocol = require("../src/protocol.js");

function request(type, payload = {}) {
  return {
    source: Protocol.DASHBOARD_SOURCE,
    protocol_version: Protocol.VERSION,
    type,
    request_id: "request_123",
    payload,
  };
}

test("protocol exposes only session bootstrap and lifecycle operations", () => {
  for (const type of ["CONNECT_SESSION", "REFRESH", "STATUS", "DISCONNECT"]) {
    const payload =
      type === "CONNECT_SESSION"
        ? {
            api_base: "https://api.tin.computer",
            project_id: "project_123",
            session_grant: "one-time-session-grant",
          }
        : {};
    assert.equal(Protocol.validateRequest(request(type, payload)).type, type);
  }
  for (const removed of ["PAIR_START", "PAIR_STATUS", "DISPATCH", "UNPAIR"]) {
    assert.throws(
      () => Protocol.validateRequest(request(removed)),
      (error) => error.code === "invalid_request",
    );
  }
});

test("CONNECT_SESSION carries a one-time grant but cannot carry cookies or device credentials", () => {
  const parsed = Protocol.validateRequest(
    request("CONNECT_SESSION", {
      api_base: "https://api.tin.computer/",
      project_id: "project_123",
      session_grant: "one-time-session-grant",
    }),
  );
  assert.deepEqual(parsed.payload, {
    api_base: "https://api.tin.computer",
    project_id: "project_123",
    session_grant: "one-time-session-grant",
  });
  for (const [field, value] of [
    ["cookies", [{ name: "li_at", value: "secret" }]],
    ["device_token", "secret"],
    ["device_token_hash", "secret"],
  ]) {
    assert.throws(
      () =>
        Protocol.validateRequest(
          request("CONNECT_SESSION", {
            api_base: "https://api.tin.computer",
            project_id: "project_123",
            session_grant: "one-time-session-grant",
            [field]: value,
          }),
        ),
      (error) => error.code === "invalid_request",
    );
  }
});

test("allows only exact Tin dashboard and API origins", () => {
  assert.equal(Protocol.isAllowedDashboardOrigin("https://tin.computer"), true);
  assert.equal(Protocol.isAllowedDashboardOrigin("https://staging.tin.computer"), true);
  assert.equal(Protocol.isAllowedDashboardOrigin("http://localhost:3000"), true);
  assert.equal(Protocol.isAllowedDashboardOrigin("http://localhost:9999"), false);
  assert.throws(
    () => Protocol.normalizeApiBase("https://api.tin.computer.evil.test"),
    (error) => error.code === "api_base_not_allowed",
  );
});

test("status sanitization is an allowlist that strips all secrets", () => {
  const sanitized = Protocol.sanitizeStatus({
    status: "connected",
    project_id: "project_123",
    actor: { display_name: "Member", profile_url: "https://linkedin.com/in/member" },
    last_synced_at: "2026-08-06T17:00:00Z",
    status_code: "session_synced",
    extension_version: "0.2.0",
    cookies: [{ name: "li_at", value: "cookie-private" }],
    device_token: "device-private",
    session_grant: "grant-private",
    api_base: "https://api.tin.computer",
  });
  assert.deepEqual(sanitized, {
    status: "connected",
    connected: true,
    project_id: "project_123",
    actor: {
      display_name: "Member",
      profile_url: "https://linkedin.com/in/member",
    },
    last_synced_at: "2026-08-06T17:00:00Z",
    status_code: "session_synced",
    extension_version: "0.2.0",
  });
  assert.doesNotMatch(JSON.stringify(sanitized), /private/);
  assert.equal(Object.hasOwn(sanitized, "api_base"), false);
});

test("checkpoint is a disconnected public status", () => {
  const sanitized = Protocol.sanitizeStatus({
    status: "checkpoint",
    project_id: "project_123",
    status_code: "linkedin_checkpoint",
  });
  assert.equal(sanitized.status, "checkpoint");
  assert.equal(sanitized.connected, false);
});
