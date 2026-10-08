"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const manifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"),
);
const storeManifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "manifest.store.json"), "utf8"),
);
const backgroundSource = fs.readFileSync(
  path.join(__dirname, "..", "src", "background.js"),
  "utf8",
);

test("Manifest V3 limits request observation to LinkedIn browser context", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.version, "0.4.3");
  assert.deepEqual(
    [...manifest.permissions].sort(),
    ["alarms", "cookies", "scripting", "storage", "webRequest"],
  );
  const serialized = JSON.stringify(manifest);
  for (const forbidden of ["tabs", "history", "debugger", "<all_urls>"]) {
    assert.equal(serialized.includes(forbidden), false, `${forbidden} must be absent`);
  }
  assert.match(backgroundSource, /\["requestHeaders", "extraHeaders"\]/);
});

test("LinkedIn has host permission for cookie capture but no page content script", () => {
  assert.equal(manifest.host_permissions.includes("https://*.linkedin.com/*"), true);
  assert.equal(manifest.content_scripts.length, 2);
  assert.equal(
    manifest.content_scripts.some((entry) =>
      entry.matches.some((pattern) => pattern.includes("linkedin.com")),
    ),
    false,
  );
  assert.deepEqual(manifest.content_scripts[0].js, [
    "src/protocol.js",
    "src/tin-bridge.js",
  ]);
});

test("development host access is limited to Tin APIs, Tin pages, and LinkedIn", () => {
  assert.deepEqual(
    new Set(manifest.host_permissions),
    new Set([
      "https://api.tin.computer/*",
      "https://app.tin.computer/*",
      "https://api.staging.tin.computer/*",
      "https://tin.computer/*",
      "https://www.tin.computer/*",
      "https://staging.tin.computer/*",
      "http://localhost/*",
      "http://127.0.0.1/*",
      "https://*.linkedin.com/*",
    ]),
  );
});

test("store manifest is an explicit beta with production-only access", () => {
  assert.equal(storeManifest.name, "Tin Computer for LinkedIn BETA");
  assert.equal(storeManifest.version, "0.4.3");
  assert.equal(
    storeManifest.description.startsWith("THIS EXTENSION IS FOR BETA TESTING."),
    true,
  );
  assert.ok(storeManifest.description.length <= 132);
  assert.deepEqual(
    new Set(storeManifest.permissions),
    new Set(["alarms", "cookies", "scripting", "storage", "webRequest"]),
  );
  assert.deepEqual(
    new Set(storeManifest.host_permissions),
    new Set([
      "https://api.tin.computer/*",
      "https://app.tin.computer/*",
      "https://tin.computer/*",
      "https://www.tin.computer/*",
      "https://*.linkedin.com/*",
    ]),
  );
  assert.equal(storeManifest.content_scripts.length, 2);
  assert.deepEqual(
    new Set(storeManifest.content_scripts[0].matches),
    new Set(["https://tin.computer/*", "https://www.tin.computer/*"]),
  );
  const serialized = JSON.stringify(storeManifest);
  for (const forbidden of ["staging.tin.computer", "localhost", "127.0.0.1", "<all_urls>"]) {
    assert.equal(serialized.includes(forbidden), false, `${forbidden} must be absent`);
  }
});

test("store manifest declares exact raster icon sizes", () => {
  const icons = {
    "16": "icons/icon-16.png",
    "32": "icons/icon-32.png",
    "48": "icons/icon-48.png",
    "128": "icons/icon-128.png",
  };
  assert.deepEqual(storeManifest.icons, icons);
  assert.deepEqual(storeManifest.action.default_icon, icons);
});
