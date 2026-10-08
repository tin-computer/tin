"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const extensionRoot = path.resolve(__dirname, "..");
const packageScript = path.join(extensionRoot, "scripts", "package_store.py");
const expectedEntries = [
  "icons/icon-128.png",
  "icons/icon-16.png",
  "icons/icon-32.png",
  "icons/icon-48.png",
  "manifest.json",
  "popup/popup.css",
  "popup/popup.html",
  "popup/popup.js",
  "src/background.js",
  "src/browser-context.js",
  "src/protocol.js",
  "src/tin-bridge.js",
  "popup/collection.html",
  "popup/collection.js",
  "popup/collection.css",
  "src/collection/core.js",
  "src/collection/page-evidence.js",
  "src/collection/linkedin.js",
  "src/collection/worker.js",
  "src/collection/bridge.js",
];

function runPython(args, options = {}) {
  const result = spawnSync("python3", args, {
    cwd: extensionRoot,
    encoding: "utf8",
    ...options,
  });
  assert.equal(
    result.status,
    0,
    `python command failed\nstdout: ${result.stdout}\nstderr: ${result.stderr}`,
  );
  return result.stdout.trim();
}

function sha256(filename) {
  return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

test("store package is deterministic and contains only the exact runtime allowlist", () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "tin-store-package-"));
  try {
    const first = path.join(temporary, "first.zip");
    const second = path.join(temporary, "second.zip");
    runPython([packageScript, "--output", first]);
    runPython([packageScript, "--output", second]);
    assert.equal(sha256(first), sha256(second));

    const inspection = runPython([
      "-c",
      [
        "import json, sys, zipfile",
        "with zipfile.ZipFile(sys.argv[1]) as archive:",
        " print(json.dumps({'names': archive.namelist(), 'manifest': json.loads(archive.read('manifest.json'))}))",
      ].join("\n"),
      first,
    ]);
    const archive = JSON.parse(inspection);
    assert.deepEqual(archive.names, expectedEntries.sort());
    assert.equal(archive.manifest.name, "Tin Computer for LinkedIn BETA");
    assert.equal(
      archive.manifest.description.startsWith("THIS EXTENSION IS FOR BETA TESTING."),
      true,
    );
    assert.equal(
      archive.names.some((name) =>
        /(?:^|\/)(?:tests?|README|package\.json|manifest\.store\.json)/i.test(name),
      ),
      false,
    );
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
