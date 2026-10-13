# Repository contribution policy

The simple path is: focused PR, passing `verify` checks, tested on staging, one Tin review, merge.
Bug fixes, docs and integration contributions do not need an approved issue first.
Discuss architecture changes before starting a large implementation.

## External contributions and the in-house fast path

`main` requires a pull request, one approval from
`@tin-computer/tin-lite-maintainers`, resolved review threads and the GitHub Actions
`verify` check. New changes dismiss stale approvals. The CODEOWNERS entry covers the
whole repository; a contributor cannot replace it for their own PR because GitHub uses
the target branch's ownership rules.

The explicitly assigned Tin maintainers (`egeozin`, `ege-em`, `demegire`, `sarbak`) can
merge their own PRs and push directly. They are the only bypass team, rather than a
blanket admin-role exception. Normal practice is still PR → green CI → merge. Their
bypass technically covers both reviews and checks, so direct pushes remain possible;
GitHub cannot enforce a different rule based only on whether that maintainer authored
the PR. Maintainers must not use the exception to accept unreviewed external work.

An independent rule blocks force-pushes and deletion of `main`, including by this team.
Any future authorized release-history rewrite needs a deliberate temporary rules change
and a verified private backup. Repository admins can edit settings; branch protection
is not a defense against an administrator intentionally changing the policy.

## Merging deploys

A merge to `main` deploys the application. When `ci` passes on `main`, the `deploy`
workflow ships that commit to staging and, if staging starts cleanly, the same commit to
production. There is no separate release step, so test before merging:

- The required `staging` status passes on its own for changes that cannot reach the
  running service (docs, tests, evals, Markdown). Anything else stays pending until that
  exact commit is deployed to staging by `deploy-staging`. Every new push resets it.
- Deploying marks the commit; it does not prove the change works. The PR's
  **Tested on staging** section says what was exercised there and what happened, and the
  reviewer judges it.
- The maintainer bypass covers this check too. Skipping staging for a service change is
  the same as deploying it untested.
- Set the repository variable `PROD_AUTO_DEPLOY=false` to stop automatic production
  deploys; merges then stop at staging. Running `deploy` by hand with a commit on `main`
  redeploys it, which is also how to roll back. Nothing rolls back on its own: a release
  may have changed the catalog for good, so a person chooses forward or back.

## CI for contributions

Use ordinary `pull_request` CI on GitHub-hosted runners. The workflow has a read-only
token, does not persist checkout credentials, and uses only disposable PostgreSQL test
credentials and mocked providers. It does not receive production secrets or run a
deployment. Superseded PR checks are cancelled; main-branch checks run independently.

Do not execute contributor code with `pull_request_target`, production environments,
privileged self-hosted runners or a secret-bearing follow-up workflow. Live-provider
acceptance stays a separate maintainer-run check against authorized test resources.
`staging-check` uses `pull_request_target` only to read a PR's changed paths and set its
status; it never checks out the code. Deploying an outside PR to staging runs it next to
real provider credentials, so a maintainer reads the diff first.

When public, use **Require approval for first-time contributors** for fork workflow
runs. Approving CI means allowing code to run in this test environment, not approving
the PR or granting production credentials. Review workflow changes before allowing a
run. Ordinary subsequent contributions do not need a second per-run permission ritual.
See [GitHub's fork approval guidance](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks).

## Repository settings

- Require the verify and staging checks, one code-owner review and resolved review threads
  on main.
- Let only `tin-lite-maintainers` update `main` ("Main: only maintainers merge"); other
  writers open pull requests and deploy branches to staging.
- Limit manual runs of `deploy` to `PROD_DEPLOYERS`.
- Restrict the `production` environment to `main`; Google Cloud trusts only that
  environment for the production deployer, and only `staging` for the staging deployer.
- Limit the review/check bypass to the named Tin maintainer team.
- Independently block force-pushes and deletion of main with no bypass actors.
- Keep the default Actions token read-only and prevent Actions from approving PRs.
- Require approval before first-time external contributors run fork CI. Never forward
  production secrets or write tokens to contributed code.

Inspect the repository's Rules and Actions settings after any repository migration.
Settings are not carried by a Git clone. A fresh source repository does not inherit
old PR history, integrations or deployment authorization.
