import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createHash } from "node:crypto";
import { checkDiagram } from "./check_diagram.mjs";
import { brandShapeFixtures } from "../web/diagram-brand-fixtures.js";
import { DOMParser } from "linkedom";

test("candidate checker rejects syntax and produces real hash-bound theme previews", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "tin-checker-test-"));
  const candidate = path.join(root, "candidate.mmd");
  await fs.writeFile(candidate, 'graph LR\nclick node "https://example.com"');
  const invalid = await checkDiagram(candidate, path.join(root, "invalid"));
  assert.equal(invalid.passed, false);
  assert.equal(invalid.renderer_sha256, undefined, "grammar fails before browser starts");
  const source = 'graph LR\n a["Start"]:::step\n b["Done"]:::receipt\n a --> b\n';
  await fs.writeFile(candidate, source);
  const valid = await checkDiagram(candidate, path.join(root, "valid"));
  assert.equal(valid.passed, true, JSON.stringify(valid.issues));
  assert.equal(valid.source_sha256, createHash("sha256").update(source).digest("hex"));
  assert.deepEqual(valid.themes.map((t) => t.theme), ["light", "dark"]);
  assert.deepEqual(valid.themes[0].quality, valid.themes[1].quality);
  assert.equal(valid.themes[0].quality.bends, 0);
  assert.equal(valid.themes[0].quality.crossings, 0);
  assert.ok(valid.themes[0].quality.fitScale > 0);
  assert.equal(valid.previews.length, 2);
  for (const image of valid.previews) assert.ok((await fs.stat(image)).size > 100);
  const svg = await fs.readFile(path.join(root, "valid", "light.svg"), "utf8");
  assert.ok(svg.includes("data:font/woff2;base64,"));
  assert.match(svg, /text-rendering="geometricPrecision"/i);
  assert.equal(await fs.readFile(candidate, "utf8"), source);
  const final = await checkDiagram(candidate, path.join(root, "final"), { previews: false });
  assert.equal(final.passed, true);
  assert.deepEqual(final.themes, valid.themes, "publication check measures the same exact layout without previews");
  assert.deepEqual(final.previews, []);
});

test("corner hierarchy survives independent checking and standalone SVG export", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "tin-shape-checker-"));
  for (const [index, fixture] of brandShapeFixtures.entries()) {
    const candidate = path.join(root, `${fixture.id}.mmd`), output = path.join(root, fixture.id);
    await fs.writeFile(candidate, fixture.source);
    const report = await checkDiagram(candidate, output);
    assert.equal(report.passed, true, JSON.stringify(report.issues));
    assert.equal(report.source_sha256, createHash("sha256").update(fixture.source).digest("hex"));
    for (const theme of ["light", "dark"]) {
      const svg = new DOMParser().parseFromString(await fs.readFile(path.join(output, `${theme}.svg`), "utf8"), "image/svg+xml");
      const actual = [".edge-label > rect", ".tin-diagram-step > rect", ".tin-diagram-group > rect"].map(selector => Number(svg.querySelector(selector).getAttribute("rx")));
      assert.deepEqual(actual, [[4,8,12], [0,0,0], [4,8,12], [6,12,16]][index]);
    }
  }
});


test("approved palette survives offline previews and portable SVG export", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "tin-brand-checker-"));
  const brand = {revision: "a".repeat(40), sha256: "b".repeat(64),
    light: {ink: "#492136", paper: "#FFF9ED", accent: "#CE9645"},
    dark: {ink: "#EDF5EF", paper: "#10241C", accent: "#58CC99"}};
  const source = `graph LR\n%% tin:brand ${JSON.stringify(brand)}\n a["Start"]:::step\n b["Evidence"]:::receipt\n a --> b\n`;
  const candidate = path.join(root, "candidate.mmd");
  await fs.writeFile(candidate, source);
  const report = await checkDiagram(candidate, path.join(root, "preview"));
  assert.equal(report.passed, true, JSON.stringify(report.issues));
  for (const [theme, paper, ink] of [["light", "rgb(255, 249, 237)", "rgb(73, 33, 54)"], ["dark", "rgb(16, 36, 28)", "rgb(237, 245, 239)"]]) {
    const svg = await fs.readFile(path.join(root, "preview", `${theme}.svg`), "utf8");
    assert.ok(svg.includes(`fill="${paper}"`), `${theme} export background`);
    assert.ok(svg.includes(`fill="${ink}"`), `${theme} export text`);
    assert.ok(svg.includes("data:font/woff2;base64,"));
    assert.ok(!svg.includes("var(--"), "export needs neither Tin nor project CSS");
  }
});
