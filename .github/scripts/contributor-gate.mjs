// Contributor gate for outside workflow pull requests. See docs/contributing-workflows.md.
// Runs from pull_request_target on the base branch's copy of this file: it reads the pull
// request through the API and never checks out or executes the contributor's code.

const GUIDE =
  "https://github.com/tin-computer/tin/blob/main/docs/contributing-workflows.md";
const TRUSTED = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);
const KEY = /^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$/;
const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

export const SECTIONS = [
  "Who runs it and what they get",
  "Closest existing workflow or open pull request, and how this differs",
  "What it reads from Tin instead of asking the founder",
  "How you tested it",
];

export const REASON_STEPS = {
  no_tin_account:
    "Your GitHub account isn't linked to a Tin account. Sign up at https://app.tin.computer " +
    "and connect GitHub to your project. Tin records the GitHub account that authorizes the " +
    "connection, so reconnect if you connected before this gate existed.",
  run_not_found: "Tin has no run with that ID. Copy the run ID from the run you started.",
  run_not_owned: "That run was started by a different Tin account from the one your GitHub account is linked to.",
  personal_project:
    "The run is in your personal project. Create a project for a real product of yours, " +
    "complete Start here there and run the workflow in that project.",
  github_not_connected: "Connect GitHub to the project the run belongs to.",
  onboarding_incomplete: "Complete Start here in that project (approve the setup it proposes).",
  run_not_finished: "The run didn't succeed. Fix the workflow, run it again and cite the new run.",
  package_mismatch:
    "The run isn't of this package's private copy. Activate it as custom.<name> (the part of " +
    "the key after the dot) and run that.",
};

export function packageKeys(files) {
  const keys = new Set();
  for (const { filename } of files) {
    const match = /^(?:workflow_packages|workflow_evals)\/([^/]+)\//.exec(filename);
    if (match) keys.add(match[1]);
  }
  return [...keys].sort();
}

export function isWorkflowPullRequest(files) {
  return packageKeys(files).length > 0;
}

function sectionText(body, heading) {
  const lines = body.split(/\r?\n/);
  const start = lines.findIndex((line) => line.replace(/^#+\s*/, "").trim() === heading);
  if (start < 0 || !/^#{2,4}\s/.test(lines[start])) return null;
  const rest = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,4}\s/.test(line)) break;
    rest.push(line);
  }
  return rest.join("\n").replace(/<!--[\s\S]*?-->/g, "").trim();
}

export function runId(body) {
  const match = /^\s*Tin run ID:\s*(\S+)/im.exec(body || "");
  const value = match && UUID.exec(match[1]);
  return value ? value[0].toLowerCase() : null;
}

// Problems a maintainer never needs to see: each closes the pull request with its fix.
export function structureProblems({ files, body, requireRun = true }) {
  const problems = [];
  const keys = packageKeys(files);
  if (keys.length > 1) {
    problems.push(`It touches ${keys.length} packages (${keys.join(", ")}). Send one package per pull request.`);
  }
  for (const key of keys) {
    if (!KEY.test(key)) {
      problems.push(`\`${key}\` isn't a valid key: use \`namespace.name\` in lowercase letters, digits and underscores.`);
    } else if (key.startsWith("example.") || key.startsWith("custom.")) {
      problems.push(`\`${key}\` uses a reserved prefix. \`example.*\` and \`custom.*\` keys can't be contributed.`);
    }
  }
  const key = keys[0];
  const outside = files
    .filter(({ filename, status }) => {
      if (filename.startsWith(`workflow_packages/${key}/`)) return false;
      if (filename.startsWith(`workflow_evals/${key}/`)) return false;
      if (/^tests\/test_[a-z0-9_]+\.py$/.test(filename) && status === "added") return false;
      return true;
    })
    .map(({ filename }) => filename);
  if (outside.length) {
    problems.push(
      "It changes files a workflow contribution doesn't own: " +
        outside.map((name) => `\`${name}\``).join(", ") +
        ". Keep to the package folder, its `workflow_evals` folder and new `tests/test_*.py` " +
        "files. Maintainers register workflows in the catalog; don't commit PR notes or " +
        "sample output.",
    );
  }
  const text = body || "";
  if (requireRun && !runId(text)) {
    problems.push("The description has no `Tin run ID: <uuid>` line for the run of your private copy.");
  }
  const empty = SECTIONS.filter((heading) => {
    const content = sectionText(text, heading);
    return content === null || content.length < 40;
  });
  if (empty.length) {
    problems.push(
      "These template sections are missing or nearly empty: " +
        empty.map((heading) => `“${heading}”`).join(", ") + ".",
    );
  }
  return problems;
}

export function closingComment({ problems = [], reasons = [] }) {
  const items = [...problems, ...reasons.map((code) => REASON_STEPS[code] || `Check failed: ${code}.`)];
  return [
    "Thanks for the contribution. This pull request was closed automatically because it " +
      "doesn't meet the requirements for workflow contributions yet:",
    "",
    ...items.map((item) => `- ${item}`),
    "",
    "We only review workflows from people who use Tin on a product of their own: a Tin " +
      "account, GitHub connected on a real project, Start here completed, and a succeeded " +
      "run of the package's private `custom.*` copy on that project.",
    "",
    `Once that's all in place, open a new pull request. The steps are in [contributing workflows](${GUIDE}).`,
  ].join("\n");
}

async function listFiles(github, repo, pull_number) {
  return github.paginate(github.rest.pulls.listFiles, { ...repo, pull_number, per_page: 100 });
}

async function otherOpenWorkflowPullRequest(github, repo, pr) {
  const open = await github.paginate(github.rest.pulls.list, { ...repo, state: "open", per_page: 100 });
  for (const other of open) {
    if (other.number === pr.number || other.user.id !== pr.user.id) continue;
    if (isWorkflowPullRequest(await listFiles(github, repo, other.number))) return other.number;
  }
  return null;
}

async function close(github, repo, number, body) {
  await github.rest.issues.createComment({ ...repo, issue_number: number, body });
  await github.rest.pulls.update({ ...repo, pull_number: number, state: "closed" });
}

async function askTin({ fetch, url, token, pr, key, run }) {
  const response = await fetch(`${url.replace(/\/$/, "")}/api/contributor-checks`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ github_user_id: pr.user.id, run_id: run, package_key: key }),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`Tin answered HTTP ${response.status}`);
  const answer = await response.json();
  if (typeof answer.verified !== "boolean" || !Array.isArray(answer.reasons)) {
    throw new Error("Tin's answer had an unexpected shape");
  }
  return answer;
}

export async function gate({ github, context, core, fetch = globalThis.fetch, env = process.env }) {
  const pr = context.payload.pull_request;
  const repo = context.repo;
  if (TRUSTED.has(pr.author_association) || pr.user.type === "Bot") {
    core.info("Maintainer or bot pull request; not gated.");
    return "skipped";
  }
  const files = await listFiles(github, repo, pr.number);
  if (!isWorkflowPullRequest(files)) {
    core.info("No workflow package changes; not gated.");
    return "skipped";
  }
  // A maintainer adds gate-exempt and reopens for packages that can't run privately yet.
  const exempt = (pr.labels || []).some((label) => label.name === "gate-exempt");
  const problems = structureProblems({ files, body: pr.body, requireRun: !exempt });
  const other = await otherOpenWorkflowPullRequest(github, repo, pr);
  if (other) {
    problems.push(`You already have an open workflow pull request (#${other}). Finish that one first.`);
  }
  if (problems.length) {
    await close(github, repo, pr.number, closingComment({ problems }));
    return "closed";
  }
  if (exempt) {
    core.info("A maintainer marked this pull request gate-exempt.");
    return "exempt";
  }
  try {
    if (!env.TIN_CONTRIBUTOR_CHECK_URL || !env.TIN_CONTRIBUTOR_CHECK_TOKEN) {
      throw new Error("the Tin check URL or token isn't configured");
    }
    const answer = await askTin({
      fetch,
      url: env.TIN_CONTRIBUTOR_CHECK_URL,
      token: env.TIN_CONTRIBUTOR_CHECK_TOKEN,
      pr,
      key: packageKeys(files)[0],
      run: runId(pr.body),
    });
    if (!answer.verified) {
      await close(github, repo, pr.number, closingComment({ reasons: answer.reasons }));
      return "closed";
    }
  } catch (error) {
    // Never close a pull request because Tin couldn't be asked; a maintainer reruns the job.
    core.warning(`Contributor check unavailable: ${error.message}`);
    await github.rest.issues.addLabels({ ...repo, issue_number: pr.number, labels: ["gate-error"] });
    return "error";
  }
  await github.rest.issues.addLabels({ ...repo, issue_number: pr.number, labels: ["tin-verified"] });
  return "verified";
}
