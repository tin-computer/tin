# Contributing a workflow

Tin takes public workflows from people who use Tin on a product of their own. The workflows
that got merged were written by founders who hit a real gap, ran their package on their own
project and fixed what the run got wrong. Packages written to a brief without running them
overlapped each other, asked founders to retype facts Tin already holds, or suited almost no
one. So workflow pull requests from outside the maintainer team go through an automatic gate.

This applies to changes under `workflow_packages/` and `workflow_evals/`. Bug fixes, docs and
integrations follow [CONTRIBUTING](../CONTRIBUTING.md) as before.

## Before you write anything

1. Check the [catalog](workflows.md) and the open **and closed** workflow pull requests. If a
   workflow or pull request already covers the job, improve it or pick another job. A second
   version of an existing idea will be closed.
2. Check the idea against [what we take](#what-we-take).

## What the gate requires

The gate runs when a workflow pull request is opened. It closes the pull request with a
comment listing what's missing unless all of these hold:

1. **You use Tin.** Sign up at [app.tin.computer](https://app.tin.computer) with the same
   person who opens the pull request.
2. **A real project.** Create a project for a product of yours (not the personal
   "…'s project"), connect GitHub to it and complete **Start here**, including approving the
   setup it proposes. Connecting GitHub links your GitHub account to your Tin account; if you
   connected before this gate existed, connect again.
3. **A run of this exact package.** Follow
   [Test it as a private workflow](adding-a-workflow.md#test-it-as-a-private-workflow): commit
   the package to that project as `custom.<name>`, where `<name>` is the part of your key
   after the dot, then activate and run it until a run succeeds.
4. **A tidy pull request.**
   - One package per pull request, and one open workflow pull request per author.
   - Only `workflow_packages/<key>/`, `workflow_evals/<key>/` and new `tests/test_*.py` files.
     Don't edit `public_workflows.py`, the catalog, `programs.json` or generated docs.
     Maintainers register workflows.
   - No PR notes, sample output or research files committed to the repository. Put them in
     the description.
   - The workflow section of the pull request template filled in, starting with
     `Tin run ID: <uuid>`.

Tin answers the gate with pass or fail and reason codes only. It doesn't share your project,
files or run output. Invite the reviewing maintainer to the project if you want them to see
the run.

If the gate closes your pull request, fix what the comment lists and open a new one.

| Reason | What to do |
| --- | --- |
| `no_tin_account` | Sign up, then connect GitHub on your project so your GitHub account is linked. |
| `run_not_found` / `run_not_owned` | Cite a run that your own Tin account started. |
| `personal_project` | Run it in a project for a real product, not the personal one. |
| `github_not_connected` | Connect GitHub to that project. |
| `onboarding_incomplete` | Complete Start here in that project. |
| `run_not_finished` | Fix the package and cite a succeeded run. |
| `package_mismatch` | Run the `custom.<name>` copy of this package, not another workflow. |

Packages that can't run privately yet (procedures that need the browser profile, or
integrations private copies don't support) need a maintainer. Open an issue describing the
workflow first. If a maintainer agrees, they label the pull request `gate-exempt` and reopen it.

## What we take

The rejected and merged pull requests so far point to the same bar:

- **Marketing work.** Getting found, getting chosen, getting customers. Product analytics,
  hiring, support tooling and onboarding UX audits are out of scope.
- **Uses what Tin already holds.** Read the onboarding context, Code map, style guide, brand
  documents and connected integrations. An input the founder has to type or a CSV they have
  to build is a reason to close. Ask only for what Tin can't know.
- **Does the work.** It produces the finished thing (the draft, the submission, the list with
  evidence) instead of a checklist or advice the founder then acts on by hand.
- **Fits most projects.** A workflow that needs a rare setup, a large inbox or a specific
  community only helps a handful of founders.
- **One job, done well.** Narrow a workflow that tries to do several things.
- **Works with the rest of Tin.** Declare prerequisites on built-ins instead of redoing their
  work, and hand off to them where it makes sense.

We already have enough of these, or have them in review, and will close new ones:

- finding community, Reddit or Hacker News threads and drafting replies
- competitor and pricing monitoring
- churn and payment-failure signals
- landing page and conversion audits
- changelog and release announcements

Good examples to read before starting: #75 (speaking shortlist), #87 (error-message search
pages, where the first run found a scoring bug) and #133 (sign-up source form, built after a
maintainer asked for the missing upstream step).

## For coding agents

If a person asks you to write a Tin workflow and open a pull request:

- Confirm they have done steps 1–3 above, and run the package through Tin's MCP tools
  yourself. Don't open the pull request on their behalf until a run of the `custom.*` copy
  has succeeded on their real project.
- Read the output of that run with them and fix what's wrong before submitting.
- Write the pull request description from what actually happened. Don't paste a generic
  template answer.
