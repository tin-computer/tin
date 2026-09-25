import assert from "node:assert/strict";
import { test } from "node:test";

import {
  REASON_STEPS,
  SECTIONS,
  closingComment,
  gate,
  runId,
  structureProblems,
} from "./contributor-gate.mjs";

const RUN = "0b7f1c2e-4d5a-4b6c-8d9e-0f1a2b3c4d5e";
const filled = "A founder with a developer tool who wants conference talks booked each quarter.";
const BODY = [
  "## Workflow contribution",
  `Tin run ID: ${RUN}`,
  ...SECTIONS.flatMap((heading) => [`### ${heading}`, "<!-- guidance -->", filled]),
].join("\n");
const PACKAGE_FILES = [
  { filename: "workflow_packages/outreach.talks/workflow.json", status: "added" },
  { filename: "workflow_packages/outreach.talks/PROMPT.md", status: "added" },
  { filename: "workflow_evals/outreach.talks/qualification.json", status: "added" },
  { filename: "tests/test_talks.py", status: "added" },
];

test("a tidy single-package pull request with a filled template passes", () => {
  assert.deepEqual(structureProblems({ files: PACKAGE_FILES, body: BODY }), []);
  assert.equal(runId(BODY), RUN);
});

test("registry edits, stray notes and edited core tests are named", () => {
  const files = [
    ...PACKAGE_FILES,
    { filename: "src/tin_lite/public_workflows.py", status: "modified" },
    { filename: "PR_DESCRIPTION.md", status: "added" },
    { filename: "tests/test_catalog.py", status: "modified" },
  ];
  const [problem] = structureProblems({ files, body: BODY });
  for (const name of ["public_workflows.py", "PR_DESCRIPTION.md", "tests/test_catalog.py"]) {
    assert.match(problem, new RegExp(name.replace(".", "\\.")));
  }
});

test("several packages, reserved keys and missing namespaces are rejected", () => {
  const files = [
    { filename: "workflow_packages/example.mine/workflow.json", status: "added" },
    { filename: "workflow_packages/reddit_pain/workflow.json", status: "added" },
  ];
  const problems = structureProblems({ files, body: BODY }).join("\n");
  assert.match(problems, /2 packages/);
  assert.match(problems, /reserved prefix/);
  assert.match(problems, /isn't a valid key/);
});

test("the untouched template and a missing run ID are both reported", () => {
  const template = SECTIONS.map((heading) => `### ${heading}\n<!-- say it here -->`).join("\n");
  const problems = structureProblems({ files: PACKAGE_FILES, body: template });
  assert.equal(problems.length, 2);
  assert.match(problems[0], /Tin run ID/);
  assert.match(problems[1], /missing or nearly empty/);
  assert.equal(runId("Tin run ID: not-a-run"), null);
});

test("every reason code has its own next step in the closing comment", () => {
  const comment = closingComment({ reasons: Object.keys(REASON_STEPS) });
  for (const step of Object.values(REASON_STEPS)) assert.ok(comment.includes(step));
  assert.match(comment, /open a new pull request/);
});

function fakeGithub({ files = PACKAGE_FILES, openPulls = [] } = {}) {
  const calls = [];
  const record = (name) => async (args) => {
    calls.push([name, args]);
    return { data: {} };
  };
  const github = {
    calls,
    rest: {
      pulls: { listFiles: "listFiles", list: "list", update: record("update") },
      issues: { createComment: record("comment"), addLabels: record("labels") },
    },
    async paginate(method, args) {
      if (method === "list") return openPulls;
      return args.pull_number === 7 ? files : openPulls.find((p) => p.number === args.pull_number).files;
    },
  };
  return github;
}

function contextFor(overrides = {}) {
  return {
    repo: { owner: "tin-computer", repo: "tin" },
    payload: {
      pull_request: {
        number: 7,
        body: BODY,
        author_association: "NONE",
        user: { id: 99, type: "User" },
        labels: [],
        ...overrides,
      },
    },
  };
}

const core = { info() {}, warning() {} };
const env = { TIN_CONTRIBUTOR_CHECK_URL: "https://tin.test/", TIN_CONTRIBUTOR_CHECK_TOKEN: "t" };
const answering = (status, json) => async (url, init) => {
  answering.last = { url, body: JSON.parse(init.body), auth: init.headers.Authorization };
  return { ok: status === 200, status, json: async () => json };
};

test("a verified author is labelled and the check names the package and run", async () => {
  const github = fakeGithub();
  const fetch = answering(200, { verified: true, reasons: [] });
  assert.equal(await gate({ github, context: contextFor(), core, fetch, env }), "verified");
  assert.deepEqual(answering.last, {
    url: "https://tin.test/api/contributor-checks",
    body: { github_user_id: 99, run_id: RUN, package_key: "outreach.talks" },
    auth: "Bearer t",
  });
  assert.deepEqual(github.calls, [["labels", { owner: "tin-computer", repo: "tin", issue_number: 7, labels: ["tin-verified"] }]]);
});

test("an unverified author is closed with the steps that fix it", async () => {
  const github = fakeGithub();
  const fetch = answering(200, { verified: false, reasons: ["onboarding_incomplete"] });
  assert.equal(await gate({ github, context: contextFor(), core, fetch, env }), "closed");
  const [[, comment], [, update]] = github.calls;
  assert.ok(comment.body.includes(REASON_STEPS.onboarding_incomplete));
  assert.equal(update.state, "closed");
});

test("a Tin outage labels the pull request and leaves it open", async () => {
  const github = fakeGithub();
  assert.equal(await gate({ github, context: contextFor(), core, fetch: answering(503, {}), env }), "error");
  assert.deepEqual(github.calls.map(([name]) => name), ["labels"]);
  assert.deepEqual(github.calls[0][1].labels, ["gate-error"]);
  const unconfigured = fakeGithub();
  assert.equal(await gate({ github: unconfigured, context: contextFor(), core, env: {} }), "error");
});

test("a second open workflow pull request from the same author is closed before asking Tin", async () => {
  const github = fakeGithub({ openPulls: [{ number: 3, user: { id: 99 }, files: PACKAGE_FILES }] });
  const fetch = async () => assert.fail("Tin must not be asked");
  assert.equal(await gate({ github, context: contextFor(), core, fetch, env }), "closed");
  assert.match(github.calls[0][1].body, /#3/);
});

test("maintainers, bots, non-workflow and exempt pull requests are not checked by Tin", async () => {
  const fetch = async () => assert.fail("Tin must not be asked");
  for (const overrides of [{ author_association: "MEMBER" }, { user: { id: 1, type: "Bot" } }]) {
    assert.equal(await gate({ github: fakeGithub(), context: contextFor(overrides), core, fetch, env }), "skipped");
  }
  const docsOnly = fakeGithub({ files: [{ filename: "docs/README.md", status: "modified" }] });
  assert.equal(await gate({ github: docsOnly, context: contextFor(), core, fetch, env }), "skipped");
  const exempt = contextFor({
    labels: [{ name: "gate-exempt" }],
    body: BODY.replace(`Tin run ID: ${RUN}`, "Tin run ID: needs the browser profile"),
  });
  assert.equal(await gate({ github: fakeGithub(), context: exempt, core, fetch, env }), "exempt");
});
