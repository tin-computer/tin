# Staging

Hosted Tin has a staging deployment at `https://app.staging.tin.computer` for running a
change end to end, as production would, before it reaches `app.tin.computer`. Local
development does not reproduce production's providers, callbacks or release process;
staging does, with its own state.

A deploy to staging is not a production deployment. Production stays a separate,
explicitly authorized step.

## Deploying

- Every push to `main` deploys to staging (`.github/workflows/deploy-staging.yml`).
- Any branch, tag or SHA deploys on request: **Actions → deploy-staging → Run workflow**,
  or `gh workflow run deploy-staging -f ref=<branch>`. One deploy runs at a time; a newer
  request waits. A branch deploy stays until the next one, so say in the team channel
  when you take staging for a branch.
- From a workstation: `TIN_LITE_DEPLOY_TARGET=staging TIN_LITE_GCP_PROJECT=<project>
  bash infra/deploy.sh`.

A staging deploy installs the host scripts, unpacks the release, runs `tin-lite migrate`
from the new release and only then switches `current` and restarts. Its database is
long-lived and never reset, so it applies every migration and catalog change in the same
order production will. A release that would crash production's boot (a changed executor,
a reused built-in ID) crashes staging's first.

The workflow authenticates to Google Cloud with workload identity. Google trusts only
jobs from this repository in the `staging` GitHub environment, and the deployer can log
in to the staging host only. The environment's variables name the provider, service
account, project and static IP.

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
