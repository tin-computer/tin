#!/usr/bin/env bash
# Writes a deploy's facts to the run page from infra/deploy.sh's log.
# Usage: deploy-summary.sh <target> <sha> <log> [outcome]
set -uo pipefail
target="$1" sha="$2" log="$3" outcome="${4:-}"
[[ -f "${log}" ]] || exit 0
field() { grep -m1 -oE "$1" "${log}" || true; }
previous="$(field 'current release: [^ ]+' | sed 's#.*/##')"
migrations="$(field 'applied migrations: .*' | sed 's/applied migrations: //')"
started="$(field 'release [0-9a-f]{40} started.*')"
startup="$(sed -nE 's/.*started after ([0-9]+)s.*/\1/p' <<<"${started}")"
host="unchanged"
grep -q "host setup changed" "${log}" && host="reinstalled (Caddy, forward proxy, units)"
failure=""
if [[ "${outcome}" != "deployed" ]]; then
  failure="$(grep -m1 -E 'did not report startup|did not start cleanly|another deploy holds|inode use|no release env|could not create a private upload|ERROR: \(gcloud|tracked worktree changes|curl: \(22\)' "${log}" || true)"
fi
{
  echo "### ${target}: ${outcome:-done}"
  echo
  echo "| | |"
  echo "| --- | --- |"
  echo "| Release | \`${sha:0:7}\` |"
  echo "| Previous release | \`${previous:0:7}\` |"
  echo "| Migrations | ${migrations:-none} |"
  echo "| Host setup | ${host} |"
  if [[ -n "${startup}" ]]; then
    echo "| Startup | reported ${startup}s after the restart began (includes the worker drain) |"
  fi
  if [[ -n "${failure}" ]]; then
    echo
    echo "**Stopped:** \`${failure:0:300}\`"
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
