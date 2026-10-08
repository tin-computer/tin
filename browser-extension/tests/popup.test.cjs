"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const popupSource = fs.readFileSync(
  path.join(__dirname, "..", "popup", "popup.js"),
  "utf8",
);
const popupHtml = fs.readFileSync(
  path.join(__dirname, "..", "popup", "popup.html"),
  "utf8",
);

function element() {
  return {
    dataset: {},
    disabled: false,
    hidden: false,
    textContent: "",
    attributes: new Map(),
    listeners: new Map(),
    setAttribute(name, value) {
      this.attributes.set(name, value);
    },
    addEventListener(name, callback) {
      this.listeners.set(name, callback);
    },
  };
}

async function renderStatus(payload) {
  const elements = Object.fromEntries(
    [
      "status-card",
      "status-dot",
      "status-title",
      "status-detail",
      "refresh",
      "disconnect",
      "disconnect-note",
      "error",
    ].map((id) => [id, element()]),
  );
  const sent = [];
  vm.runInNewContext(popupSource, {
    document: {
      getElementById(id) {
        return elements[id];
      },
    },
    chrome: {
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          sent.push(message);
          callback({ ok: true, payload });
        },
      },
    },
    Error,
    Promise,
  });
  await Promise.resolve();
  await Promise.resolve();
  return { elements, sent };
}

test("connected popup names the LinkedIn member and offers disconnect", async () => {
  const { elements, sent } = await renderStatus({
    status: "connected",
    project_id: "project_member",
    actor: { display_name: "Member Example" },
  });

  assert.equal(sent[0].namespace, "tin.linkedin.popup.v2");
  assert.equal(sent[0].type, "STATUS");
  assert.equal(elements["status-title"].textContent, "Connected to Tin");
  assert.equal(
    elements["status-detail"].textContent,
    "Session synced as Member Example. Tin can run delegated outreach while Chrome is closed.",
  );
  assert.equal(elements.disconnect.hidden, false);
  assert.equal(elements["disconnect-note"].hidden, false);
  assert.equal(elements["status-card"].attributes.get("aria-busy"), "false");
});

test("signed-out popup gives one clear recovery step", async () => {
  const { elements } = await renderStatus({
    status: "signed_out",
    project_id: "project_member",
  });
  assert.equal(elements["status-title"].textContent, "Sign in to LinkedIn");
  assert.equal(
    elements["status-detail"].textContent,
    "Sign in in this Chrome profile, then choose Refresh.",
  );
});

test("checkpoint popup never claims the session is connected", async () => {
  const { elements } = await renderStatus({
    status: "checkpoint",
    project_id: "project_member",
    status_code: "linkedin_checkpoint",
  });
  assert.equal(
    elements["status-title"].textContent,
    "LinkedIn security check needed",
  );
  assert.equal(
    elements["status-detail"].textContent,
    "Complete LinkedIn's security check in this Chrome profile, then choose Refresh.",
  );
  assert.notEqual(elements["status-title"].textContent, "Connected to Tin");
});

test("not-connected popup directs setup to Tin Integrations", async () => {
  const { elements } = await renderStatus({ status: "not_connected" });
  assert.equal(elements["status-title"].textContent, "Not connected");
  assert.match(elements["status-detail"].textContent, /Tin's LinkedIn integration/);
  assert.equal(elements.disconnect.hidden, true);
  assert.equal(elements["disconnect-note"].hidden, true);
});

test("popup makes consent and browser-only disconnect semantics explicit", () => {
  assert.match(popupHtml, /After you choose Allow outreach in Tin/);
  assert.match(popupHtml, /Stop browser sync/);
  assert.match(popupHtml, /does not remove an\s+existing project delegation/);
  assert.match(popupHtml, /aria-atomic="true"/);
  assert.match(popupHtml, /aria-describedby="disconnect-note"/);
});
