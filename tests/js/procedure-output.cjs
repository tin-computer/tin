const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../src/tin_lite/static/app.js"), "utf8");
function extract(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0, name);
  const end = source.slice(start + 1).search(/\n(?:async )?function /);
  assert.ok(end >= 0, name);
  return source.slice(start, start + end + 1);
}
const run = { id: "run", status: "failed", retained_output: {
  artifact_path: "reports/RESULT.md", revision: "saved", reason: "output_conflict",
} };
const calls = [];
let actions;
const context = vm.createContext({
  state: { runs: [run], workflows: [], runWorkflows: new Map(), view: "workflows", documentCache: new Map(), documentRoute: null },
  main: {},
  window: { TinMarkdownViewer: { mount: (_main, _doc, options) => { actions = options; } },
    setTimeout: () => {} },
  ALLOWED_VIEWS: new Set(["chat", "workflows", "activity", "decisions", "files", "integrations"]),
  URLSearchParams,
  isCampaignRevisionReview: () => false,
  supportsArticleFeedback: () => false,
  repositoryDeliveryAvailable: () => false,
  openDocument: (...args) => calls.push(args),
  openOutputComparison: (...args) => calls.push(["compare", ...args]),
  goToRoute: route => calls.push(route),
  showToast: (message) => { throw new Error(message); },
});
vm.runInContext([
  "workflowForRun", "availableRunOutput", "outputReadUrl", "retainedOutputMessage", "isMarkdownPath",
  "openRunOutputFile", "openRunArtifact", "hasOutputConflict", "runFingerprint", "renderDocument",
].map(extract).join("\n"), context);
assert.equal(context.availableRunOutput(run).source, "retained");
context.openRunArtifact("run");
assert.deepEqual(calls.pop(), ["compare", "run", "workflows"]);
const before = context.runFingerprint(run);
run.retained_output.reason = "reconciliation_pending";
assert.notEqual(context.runFingerprint(run), before);
context.openRunArtifact("run");
assert.deepEqual(calls.pop(), ["run", "workflows", "retained"]);
context.state.documentRoute = { runId: "run", source: "retained" };
context.state.documentCache.set("run:run:retained", { markdown: "saved" });
run.status = "needs_input"; // Even a stale status must not attach approval to saved output.
context.renderDocument();
assert.equal(actions.primaryAction, null);
assert.equal(actions.secondaryAction, null);
context.state.documentRoute.source = "canonical";
context.state.documentCache.set("run:run:canonical", { markdown: "canonical" });
context.renderDocument();
assert.equal(actions.primaryAction.label, "Approve draft");
run.workflow_id = "pair-template";
context.state.workflows.push({id: "pair-template", definition: {procedure: {output: {apply_on_approval: {primary: "brand/BRAND.md", companion: "DESIGN.md"}}}}});
context.renderDocument();
assert.equal(actions.primaryAction.label, "Use documents");
run.canonical_commit_sha = "published";
run.artifact_path = "reports/RESULT.md";
run.status = "failed";
assert.equal(context.availableRunOutput(run).source, "canonical");
assert.equal(context.outputReadUrl("run", "retained"), "/api/workflows/runs/run/artifact?source=retained");

// Every non-Markdown output opens in the in-app file viewer instead of the Markdown reader.
const sha = "a".repeat(40);
const saved = "b".repeat(40);
const media = { id: "media", status: "succeeded", artifact_path: "studio/character.svg", canonical_commit_sha: sha };
context.state.runs.push(media);
context.openRunArtifact("media", "decisions");
assert.equal(
  calls.pop(),
  `file?${new URLSearchParams({ path: "studio/character.svg", revision: sha, back: "decisions" })}`,
);
media.status = "needs_input"; // A review of a media artifact carries the run so the viewer can approve it.
context.openRunArtifact("media", "workflows");
assert.equal(
  calls.pop(),
  `file?${new URLSearchParams({ path: "studio/character.svg", revision: sha, reviewRun: "media", back: "workflows" })}`,
);
const savedVideo = { id: "video", status: "failed", retained_output: {
  artifact_path: "studio/demo.mp4", revision: saved, reason: "reconciliation_pending",
} };
context.state.runs.push(savedVideo);
context.openRunArtifact("video", "activity");
assert.equal(
  calls.pop(),
  `file?${new URLSearchParams({ path: "studio/demo.mp4", revision: saved, compareRun: "video", source: "retained", back: "activity" })}`,
);
const pending = { id: "pending", status: "running" };
context.state.runs.push(pending);
context.openRunArtifact("pending", "chat");
assert.deepEqual(calls.pop(), ["pending", "chat", "canonical"]);
