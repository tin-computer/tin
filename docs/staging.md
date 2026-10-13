# Staging

Hosted Tin has a staging deployment at `https://app.staging.tin.computer` for running a
change end to end, as production would, before it reaches `app.tin.computer`. Local
development does not reproduce production's providers, callbacks or release process;
staging does, with its own state.

Merging to `main` deploys to production, so staging is where a change is tested first.

## Testing a change before merging

1. Deploy the branch: `git push -f origin my-branch:staging`, or **Actions →
   deploy-staging → Run workflow** with its ref (`refs/pull/<n>/head` for a fork; read the
   diff first, since staging holds real provider credentials).
2. Use the changed behavior on `app.staging.tin.computer` with a staging test project.
3. Write what you did and saw under **Tested on staging** in the pull request.

Deploying marks that exact commit with the `staging` status that `main` requires. A new
push resets it. Changes that cannot reach the running service (docs, tests, evals,
Markdown) pass without a deploy. Staging has one slot: whoever deployed last owns it until
the next deploy, and a merge to `main` also takes it, so say in the team channel when you
are testing there.

## From merge to production

When `ci` passes on `main`, the `deploy` workflow deploys that commit to staging and, if it
starts cleanly, the same commit to production. Every deploy, from CI or a workstation
(`TIN_LITE_DEPLOY_TARGET=staging|prod TIN_LITE_GCP_PROJECT=<project> bash infra/deploy.sh`),
goes through `infra/activate_release.sh` on the host:

- It takes a host lock, so deploys by people and CI wait for each other.
- It stops if inode use is above 90%.
- It reinstalls host setup (Caddy, forward proxy, units) only when those scripts changed,
  because that restarts the proxy in-flight sandboxes use.
- It unpacks the release with the host's `/etc/tin-lite/release.env`, runs
  `tin-lite migrate`, switches `current` and restarts, which drains the Temporal worker first.
- It waits for the new process to log "Application startup complete" and fails if it
  restarts instead. `/healthz` alone is not proof: the old process answers during the drain.
- It keeps the newest 20 releases.

A failed deploy stops and reports; nothing rolls back on its own. Staging's database is
long-lived and never reset, so it applies every migration and catalog change in the order
production will, and a release that would crash production's boot (a changed executor, a
reused built-in ID) crashes staging's first and never reaches production.

Set the repository variable `PROD_AUTO_DEPLOY=false` to stop at staging. Run **deploy** by
hand with a commit on `main` to redeploy it or roll back.

Both deployers authenticate with workload identity. Google trusts only this repository's
`staging` environment for the staging deployer and its `production` environment (restricted
to `main`) for the production deployer; each can log in to its own host only.

## What is separate and what is shared

| | Staging |
|---|---|
| Host | Its own VM, installed by the same `install_switchboard.sh` |
| Postgres | Local to the staging host (`install_staging_postgres.sh`) |
| Temporal | Its own namespace |
| code.storage | Production's organization, its own key, every repository prefixed `staging-` |
| Clerk | A development instance |
| GitHub | Its own GitHub App and founder-account OAuth App |
| Stripe | Test mode, with its own webhook endpoint |
| Keyword Planner (gak) | Its own service on the staging host |
| Models, E2B, data providers | Production's accounts and keys |
| Google and X OAuth | Production's clients, once the staging callbacks are registered |

`TIN_LITE_ENVIRONMENT=staging` refuses to boot without `TIN_LITE_CODE_STORAGE_REPO_PREFIX`,
outside Stripe test mode, or when the database host or Temporal namespace equals
production's (`TIN_LITE_PRODUCTION_DATABASE_HOST`, `TIN_LITE_PRODUCTION_TEMPORAL_NAMESPACE`).

The prefix applies wherever Tin names a repository upstream: lookups, creation, deletion,
sandbox git remotes and scoped tokens. Stored references keep the unprefixed ID, so
staging writes `staging-registry/workflows` and `staging-projects/<id>` and never
production's repositories. The key is still organization-wide; rotate it alone if the
staging host is compromised.

## Where its configuration lives

On the staging host, readable by root and the service group only:

- `/etc/tin-lite/release.env`: secrets and settings, copied into each release as `.env`.
  Nothing is uploaded from a workstation.
- `/etc/tin-lite/clerk.env` and `/etc/tin-lite/runtime.env`: the service environment.
  `install_switchboard.sh` regenerates the hosts and proxy keys in `runtime.env` and keeps
  every other line.
- `/etc/tin-lite/github-app.pem` and `/etc/tin-lite/postgres-password`.

## How it differs from production

- Production's Postgres refuses `DELETE` and `UPDATE` without `WHERE` (`pg_strict`);
  staging's does not. `tests/test_sql_requires_where.py` catches such statements in CI.
- The database is on the same host: no network latency, failover or connection drops.
  Its settings otherwise follow production's (version, `max_connections`, time zone,
  memory, a role that is not a superuser).
- Clerk's development instance keeps sessions differently and is shared with the Tin
  website's staging site, so its sign-in paths point there. Tin's own sign-in works; an
  MCP connection's sign-in and consent use the instance's hosted pages.
- Real model, sandbox and data-provider calls cost real money. Keep test projects few and
  their spending limits low; pause schedules you are not testing.

Fixture tests prove the contract and staging proves the release; production still needs
its one real run after a production deploy.
