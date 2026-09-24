#!/usr/bin/env bash
set -euo pipefail

umask 077

# Capability probe before credentials, cloning or model dispatch.
if [[ "${1:-}" == "--check-reviewed-documents" ]]; then
  printf 'TIN_PROCEDURE_DOCUMENTS_V1\n'
  exit 0
fi
if [[ "${1:-}" == "--check-editorial" ]]; then
  printf 'TIN_PROCEDURE_EDITORIAL_V1\n'
  exit 0
fi
if [[ "${1:-}" == "--check-companion" ]]; then
  printf 'TIN_PROCEDURE_COMPANION_V1\n'
  exit 0
fi

if [[ -n "${OPENAI_API_KEY:-}" || -n "${CODEX_API_KEY:-}" || \
      -n "${TIN_LITE_LUNA_API_KEY:-}" || -n "${ANTHROPIC_API_KEY:-}" || \
      -n "${GEMINI_API_KEY:-}" || -n "${OPENROUTER_API_KEY:-}" || \
      -n "${TIN_LITE_INTEGRATION_CREDENTIAL_KEY:-}" || \
      -n "${TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET:-}" || \
      -n "${TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH:-}" || \
      -n "${TIN_LITE_GITHUB_WEBHOOK_SECRET:-}" || -n "${FAL_KEY:-}" ]]; then
  echo "API-key authentication is forbidden in a Codex procedure sandbox" >&2
  exit 70
fi

required=(
  TIN_EXECUTION_KEY TIN_SANDBOX_ID
  TIN_CANONICAL_URL TIN_CANONICAL_AUTH_HEADER TIN_CANONICAL_BRANCH
  TIN_EPHEMERAL_URL TIN_EPHEMERAL_AUTH_HEADER TIN_EPHEMERAL_BRANCH
  TIN_PROCEDURE_OUTPUT_PATH TIN_PROCEDURE_OUTPUT_MAX_BYTES
)
if [[ "${TIN_PROCEDURE_ISOLATED:-}" != "1" || -n "${TIN_BROKER_GRANT:-}" ||
      -z "${TIN_CODEX_API_URL:-}" || -z "${TIN_CODEX_API_GRANT:-}" ]]; then
  echo "Codex runs require protected API execution" >&2
  exit 64
fi
required+=(TIN_CODEX_API_URL TIN_CODEX_API_GRANT)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "missing required procedure setting: ${name}" >&2
    exit 64
  fi
done
# The context arrives as a file (TIN_PROCEDURE_CONTEXT_PATH) or, from older switchboards,
# as one base64 variable. Linux caps a single environment string at 128 KiB, which a large
# procedure package plus its inputs can exceed; the file has no such limit.
if [[ -n "${TIN_PROCEDURE_CONTEXT_PATH:-}" ]]; then
  if [[ ! -r "${TIN_PROCEDURE_CONTEXT_PATH}" ]]; then
    echo "procedure context file is missing: ${TIN_PROCEDURE_CONTEXT_PATH}" >&2
    exit 64
  fi
elif [[ -z "${TIN_PROCEDURE_CONTEXT_B64:-}" ]]; then
  echo "missing required procedure setting: TIN_PROCEDURE_CONTEXT_PATH" >&2
  exit 64
fi

if [[ "${TIN_PROCEDURE_OUTPUT_PATH}" == /* || \
      "${TIN_PROCEDURE_OUTPUT_PATH}" == *".."* || \
      "${TIN_PROCEDURE_OUTPUT_PATH}" == *\\* ]]; then
  echo "unsafe procedure output path" >&2
  exit 64
fi
if [[ ! "${TIN_PROCEDURE_OUTPUT_MAX_BYTES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid procedure artifact byte limit" >&2
  exit 64
fi
# The result kind decides what the post-Codex checks look at. Older switchboards did not
# export it, so an archive without a declared kind keeps the pull-request behavior.
result_kind="${TIN_PROCEDURE_RESULT_KIND:-}"
if [[ -z "${result_kind}" ]]; then
  if [[ -n "${TIN_PROCEDURE_WORKSPACE_ARCHIVE:-}" ]]; then
    result_kind="github.pull_request"
  else
    result_kind="project.artifact"
  fi
fi
if [[ "${result_kind}" != "project.artifact" && "${result_kind}" != "github.pull_request" ]]; then
  echo "unsupported procedure result kind" >&2
  exit 64
fi
output_paths=("${TIN_PROCEDURE_OUTPUT_PATH}")
if [[ -n "${TIN_PROCEDURE_COMPANION_PATH:-}" ]]; then
  if [[ "${result_kind}" != "project.artifact" ]] || {
      [[ "${TIN_PROCEDURE_DOCUMENT_PAIR:-}" != "1" ]] &&
      [[ "${TIN_PROCEDURE_COMPANION_PATH}" != "${TIN_PROCEDURE_OUTPUT_PATH%.md}.generation.md" ]]; }; then
    echo "invalid procedure companion path" >&2
    exit 64
  fi
  output_paths+=("${TIN_PROCEDURE_COMPANION_PATH}")
fi
if [[ -n "${TIN_RUN_TOOLS_URL:-}" || -n "${TIN_RUN_TOOLS_GRANT:-}" ]]; then
  if [[ -z "${TIN_RUN_TOOLS_URL:-}" || -z "${TIN_RUN_TOOLS_GRANT:-}" ]]; then
    echo "incomplete run-tool configuration" >&2
    exit 64
  fi
  /usr/local/bin/codex mcp remove tin-run >/dev/null 2>&1 || true
  /usr/local/bin/codex mcp add tin-run \
    --url "${TIN_RUN_TOOLS_URL}" \
    --bearer-token-env-var TIN_RUN_TOOLS_GRANT >/dev/null
fi
if [[ "${TIN_PROCEDURE_BROWSER:-}" == "1" ]]; then
  # Browser profile: the Tin-owned camoufox-mcp server launches one persistent Camoufox
  # (fingerprint-hardened Firefox) for the whole run. The browser egresses through the
  # local Cloudflare WARP SOCKS proxy that warp-up starts, because E2B egress is IPv4-only
  # and Cloudflare serves Turnstile challenge dependencies from IPv6-only hostnames. Codex
  # still reaches OpenAI through the switchboard proxy env vars.
  for tool in /opt/tin-lite/warp-up /opt/tin-lite/camoufox-mcp \
              /opt/tin-lite/cfx-venv/bin/python; do
    if [[ ! -x "${tool}" ]]; then
      echo "browser profile requires ${tool} in the sandbox template" >&2
      exit 69
    fi
  done
  mkdir -p /home/user/.tin-lite/browser/profile
  /opt/tin-lite/warp-up /home/user/.tin-lite/browser >/dev/null
  # The MCP server runs with its working directory outside the project checkout so any
  # file it saves can never count as a project change.
  /usr/local/bin/codex mcp remove camoufox >/dev/null 2>&1 || true
  /usr/local/bin/codex mcp add camoufox \
    --env TIN_BROWSER_PROFILE_DIR=/home/user/.tin-lite/browser/profile \
    -- /bin/bash -c 'cd /home/user/.tin-lite/browser && exec "$0" "$@"' \
      /opt/tin-lite/cfx-venv/bin/python /opt/tin-lite/camoufox-mcp >/dev/null
  # Codex drops an MCP server that misses its 10-second default handshake and cancels tool
  # calls after 60 seconds; the browser launches on the first call and page loads wait up
  # to a minute, so both limits are raised for this server only.
  sed -i '/^\[mcp_servers.camoufox\]$/a startup_timeout_sec = 60\ntool_timeout_sec = 180' \
    /home/user/.codex/config.toml
fi
if [[ "${TIN_PROCEDURE_STUDIO:-}" == "1" ]]; then
  # Studio profile: the `tin-studio` toolkit captures the founder's product through its own
  # Camoufox, renders the demo, and asks the switchboard for voice through the run-bound grant.
  # The provider credential never enters the sandbox.
  for tool in /opt/tin-lite/studio/tin-studio /opt/tin-lite/studio-venv/bin/python \
              /opt/tin-lite/studio/resvg /usr/bin/ffmpeg; do
    if [[ ! -x "${tool}" ]]; then
      echo "studio profile requires ${tool} in the sandbox template" >&2
      exit 69
    fi
  done
  if [[ -z "${TIN_RUN_TOOLS_URL:-}" || -z "${TIN_RUN_TOOLS_GRANT:-}" ]]; then
    echo "studio profile requires the run-bound studio voice grant" >&2
    exit 64
  fi
  mkdir -p /home/user/.tin-lite/studio
  if [[ "${TIN_PROCEDURE_ISOLATED:-}" == "1" ]]; then
    # Only the controller can create this file. The root-owned helper passes just
    # the run-scoped voice grant to the toolkit, not the model or storage grants.
    python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path('/home/user/.tin-lite/studio-worker.json')
path.write_text(json.dumps({key: os.environ[key] for key in (
    'TIN_RUN_TOOLS_URL', 'TIN_RUN_TOOLS_GRANT',
)}))
path.chmod(0o600)
PY
  fi
fi

workspace=/home/user/project
state_workspace=/home/user/project
# Codex writes rollouts under /home/user/.codex/sessions; the switchboard captures them
# before the sandbox is killed. Cleanup must never remove that directory.
result_file=/home/user/.tin-lite/procedure-result.json

cleanup() {
  exit_code=$?
  trap - EXIT
  rm -f "${result_file}" \
    /home/user/.tin-lite/procedure-workspace.tar.gz \
    /home/user/.tin-lite/open-pull-requests.json
  exit "${exit_code}"
}
trap cleanup EXIT

mkdir -p /home/user/.codex /home/user/.tin-lite
python3 /opt/tin-lite/codex_api_config.py

rm -rf "${workspace}" /home/user/state
if [[ -n "${TIN_PROCEDURE_WORKSPACE_ARCHIVE:-}" ]]; then
  state_workspace=/home/user/state
fi
GIT_TERMINAL_PROMPT=0 \
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=http.extraHeader \
GIT_CONFIG_VALUE_0="${TIN_CANONICAL_AUTH_HEADER}" \
  git clone --filter=blob:none --single-branch --branch "${TIN_CANONICAL_BRANCH}" \
    "${TIN_CANONICAL_URL}" "${state_workspace}" >/dev/null

# Reviewed diagrams read one captured project revision, even if the canonical
# branch advanced while their sandbox was starting. Older procedures are unchanged.
if [[ -n "${TIN_PROCEDURE_PROJECT_REVISION:-}" ]]; then
  if [[ ! "${TIN_PROCEDURE_PROJECT_REVISION}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "invalid pinned project revision" >&2
    exit 64
  fi
  for operation in fetch checkout; do
    args=(fetch origin "${TIN_PROCEDURE_PROJECT_REVISION}")
    if [[ "${operation}" == checkout ]]; then
      args=(checkout --detach "${TIN_PROCEDURE_PROJECT_REVISION}")
    fi
    GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="${TIN_CANONICAL_AUTH_HEADER}" \
      git -C "${state_workspace}" "${args[@]}" >/dev/null
  done
fi

git -C "${state_workspace}" remote add ephemeral "${TIN_EPHEMERAL_URL}"
git -C "${state_workspace}" config user.name "Tin Procedure Agent"
git -C "${state_workspace}" config user.email "procedure-agent@tin.local"

set +e
remote_line="$(
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0=http.extraHeader \
  GIT_CONFIG_VALUE_0="${TIN_EPHEMERAL_AUTH_HEADER}" \
    git -C "${state_workspace}" ls-remote --exit-code \
      ephemeral "refs/heads/${TIN_EPHEMERAL_BRANCH}" 2>/dev/null
)"
remote_status=$?
set -e
if [[ "${remote_status}" -eq 0 ]]; then
  commit_sha="${remote_line%%[[:space:]]*}"
  GIT_TERMINAL_PROMPT=0 \
  GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0=http.extraHeader \
  GIT_CONFIG_VALUE_0="${TIN_EPHEMERAL_AUTH_HEADER}" \
    git -C "${state_workspace}" fetch --quiet ephemeral "${commit_sha}"
  changed="$(git -C "${state_workspace}" diff-tree --no-commit-id --name-only -r "${commit_sha}")"
  unexpected="$(printf '%s\n' "${changed}" | grep -Fvx -f <(printf '%s\n' "${output_paths[@]}") || true)"
  if [[ -n "${unexpected}" ]]; then
    echo "ephemeral recovery branch is not a valid procedure result" >&2
    exit 67
  fi
  for output_path in "${output_paths[@]}"; do
    output_size="$(git -C "${state_workspace}" cat-file -s "${commit_sha}:${output_path}" 2>/dev/null || true)"
    if [[ ! "${output_size}" =~ ^[1-9][0-9]*$ ]]; then
      echo "ephemeral recovery branch is missing an output" >&2
      exit 67
    fi
  done
  printf '%s' '{"summary":"Recovered the completed procedure artifact.","message":"The procedure artifact is ready."}' \
    > "${result_file}"
  printf 'TIN_PROCEDURE_COMMIT_SHA=%s\n' "${commit_sha}"
  base64 -w 0 "${result_file}" | sed 's/^/TIN_PROCEDURE_RESULT=/'
  printf '\n'
  exit 0
elif [[ "${remote_status}" -ne 2 ]]; then
  echo "could not inspect the procedure checkpoint branch" >&2
  exit 68
fi

if [[ -n "${TIN_PROCEDURE_WORKSPACE_ARCHIVE:-}" ]]; then
  if [[ "${TIN_PROCEDURE_WORKSPACE_ARCHIVE}" != "/home/user/.tin-lite/procedure-workspace.tar.gz" || \
        ! -s "${TIN_PROCEDURE_WORKSPACE_ARCHIVE}" ]]; then
    echo "invalid procedure repository workspace" >&2
    exit 64
  fi
  if [[ "${result_kind}" == "github.pull_request" ]] && {
    [[ "${TIN_PROCEDURE_WORKSPACE_EVIDENCE:-}" != "/home/user/.tin-lite/open-pull-requests.json" || \
        ! -s "${TIN_PROCEDURE_WORKSPACE_EVIDENCE}" ]] ||
    (( $(wc -c < "${TIN_PROCEDURE_WORKSPACE_EVIDENCE:-/dev/null}") > 250000 )); }; then
    echo "invalid procedure open pull-request evidence" >&2
    exit 64
  fi
  mkdir -p "${workspace}"
  tar --extract --gzip --file "${TIN_PROCEDURE_WORKSPACE_ARCHIVE}" \
    --directory "${workspace}" --no-same-owner --no-same-permissions
  git -C "${workspace}" init --quiet
  git -C "${workspace}" config user.name "Tin Procedure Agent"
  git -C "${workspace}" config user.email "procedure-agent@tin.local"
  git -C "${workspace}" add --all
  git -C "${workspace}" commit --quiet -m "Pinned integration workspace"
fi

metadata_base_head=""
if [[ "${result_kind}" == "github.pull_request" ]]; then
  metadata_base_head="$(git -C "${workspace}" rev-parse HEAD)"
fi

TIN_PROCEDURE_RESULT_PATH="${result_file}" \
TIN_PROCEDURE_STATE_DIR="${state_workspace}" \
TIN_PROCEDURE_RESULT_KIND="${result_kind}" \
  env -u OPENAI_API_KEY -u CODEX_API_KEY -u TIN_LITE_LUNA_API_KEY \
    -u ANTHROPIC_API_KEY -u GEMINI_API_KEY -u OPENROUTER_API_KEY \
    -u TIN_LITE_INTEGRATION_CREDENTIAL_KEY \
    -u TIN_LITE_GOOGLE_OAUTH_CLIENT_SECRET -u TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH \
    -u TIN_LITE_GITHUB_WEBHOOK_SECRET -u FAL_KEY \
    /opt/tin-lite/procedure-app-server

if [[ "${result_kind}" == "github.pull_request" ]]; then
  if [[ "$(git -C "${workspace}" rev-parse HEAD)" != "${metadata_base_head}" ]]; then
    echo "The pinned repository baseline changed" >&2
    exit 67
  fi
  while IFS= read -r command; do
    [[ -z "${command}" ]] && continue
    # The isolated bridge already ran verification as the credential-free author UID.
    [[ "${TIN_PROCEDURE_ISOLATED:-}" == "1" ]] && continue
    (cd "${workspace}" && bash -lc "${command}")
  done < <(python3 - <<'PY'
import base64, json, os
context_path = os.environ.get("TIN_PROCEDURE_CONTEXT_PATH")
if context_path:
    context = json.loads(open(context_path, "rb").read())
else:
    context = json.loads(base64.b64decode(os.environ["TIN_PROCEDURE_CONTEXT_B64"], validate=True))
for command in context.get("verification", {}).get("commands", []):
    print(command)
PY
  )
  python3 - <<'PY'
import base64
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

workspace = Path("/home/user/project")
state_workspace = Path("/home/user/state")
context_path = os.environ.get("TIN_PROCEDURE_CONTEXT_PATH")
if context_path:
    context = json.loads(open(context_path, "rb").read())
else:
    context = json.loads(base64.b64decode(os.environ["TIN_PROCEDURE_CONTEXT_B64"], validate=True))
result = json.loads(Path("/home/user/.tin-lite/procedure-result.json").read_text())
output = context["output"]
workspace_context = context["workspace"]

def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(workspace), *args], text=True)

if git("diff", "--name-only", "--diff-filter=DR", "HEAD").strip():
    raise SystemExit("Codex deleted or renamed repository files")
changed = set(git("diff", "--name-only", "--diff-filter=ACM", "HEAD").splitlines())
changed.update(git("ls-files", "--others", "--exclude-standard").splitlines())
paths = sorted(path for path in changed if path)
max_files = output["max_files"]
no_change = (output.get("repair_policy") or output.get("allow_no_change")) and result.get(
    "outcome"
) == "no_change"
if not (len(paths) == 0 if no_change else 1 <= len(paths) <= max_files):
    raise SystemExit("Codex changed an invalid number of repository files")
files = []
total = 0
for value in paths:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or value.startswith(".github/workflows/")
        or value == ".gitmodules"
    ):
        raise SystemExit("Codex changed a protected repository path")
    try:
        content = (workspace / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit("Codex produced an unreadable repository file") from exc
    total += len(content.encode("utf-8"))
    files.append({"path": value, "content": content})
if total > output["max_bytes"]:
    raise SystemExit("Codex repository changes exceed the byte limit")
title = result.get("title")
body = result.get("body")
if not isinstance(title, str) or not title.strip() or len(title) > 200:
    raise SystemExit("Codex did not return a valid pull-request title")
if not isinstance(body, str) or len(body) > 20000:
    raise SystemExit("Codex did not return a valid pull-request body")
manifest = {
    "repository": workspace_context["repository"],
    "default_branch": workspace_context["default_branch"],
    "head_sha": workspace_context["head_sha"],
    "title": title.strip(),
    "body": body,
    "files": files,
    "verification": context.get("verification", {}).get("commands", []),
}
if output.get("repair_policy"):
    manifest.update({"outcome": result.get("outcome"), "reason": result.get("reason")})
elif output.get("allow_no_change"):
    manifest["outcome"] = result.get("outcome")
destination = state_workspace / os.environ["TIN_PROCEDURE_OUTPUT_PATH"]
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
PY
else
  if [[ "${workspace}" != "${state_workspace}" ]]; then
    # A read-only repository snapshot: the artifact belongs in the project-state checkout.
    # A model that wrote it relative to its working directory is corrected here, and any
    # other change to the snapshot is discarded because nothing is ever pushed from it.
    if [[ ! -s "${state_workspace}/${TIN_PROCEDURE_OUTPUT_PATH}" && \
          -s "${workspace}/${TIN_PROCEDURE_OUTPUT_PATH}" ]]; then
      mkdir -p "$(dirname "${state_workspace}/${TIN_PROCEDURE_OUTPUT_PATH}")"
      mv "${workspace}/${TIN_PROCEDURE_OUTPUT_PATH}" "${state_workspace}/${TIN_PROCEDURE_OUTPUT_PATH}"
    fi
    repo_changes="$(git -C "${workspace}" status --porcelain | wc -l)"
    if (( repo_changes > 0 )); then
      echo "Codex changed ${repo_changes} path(s) in the read-only repository snapshot; discarded" >&2
    fi
  fi
  for output_path in "${output_paths[@]}"; do
  if [[ ! -f "${state_workspace}/${output_path}" || -L "${state_workspace}/${output_path}" || ! -s "${state_workspace}/${output_path}" ]]; then
    echo "Codex did not create the declared procedure artifact" >&2
    exit 65
  fi
  artifact_bytes="$(wc -c < "${state_workspace}/${output_path}")"
  artifact_limit="${TIN_PROCEDURE_OUTPUT_MAX_BYTES}"
  if [[ "${output_path}" == "${TIN_PROCEDURE_COMPANION_PATH:-}" ]]; then artifact_limit="${TIN_PROCEDURE_COMPANION_MAX_BYTES:-24000}"; fi
  if (( artifact_bytes > artifact_limit )); then
    echo "Codex procedure artifact exceeds its declared byte limit" >&2
    exit 65
  fi
  done
  unexpected="$({
    git -C "${state_workspace}" status --porcelain --untracked-files=all | cut -c4- |
      grep -Fvx -f <(printf '%s\n' "${output_paths[@]}")
  } || true)"
  if [[ -n "${unexpected}" ]]; then
    echo "Codex changed files outside the procedure output contract:" >&2
    printf '%s\n' "${unexpected}" | head -n 20 >&2
    exit 66
  fi
  if ! python3 - "${state_workspace}/${TIN_PROCEDURE_OUTPUT_PATH}" <<'PY'
import base64, json, os, sys
context_path = os.environ.get("TIN_PROCEDURE_CONTEXT_PATH")
if context_path:
    context = json.loads(open(context_path, "rb").read())
else:
    context = json.loads(base64.b64decode(os.environ["TIN_PROCEDURE_CONTEXT_B64"], validate=True))
secret = (context.get("identity") or {}).get("password")
if secret and secret.encode("utf-8") in open(sys.argv[1], "rb").read():
    raise SystemExit("procedure output contains the test identity secret")
sys.path.insert(0, "/opt/tin-lite")
from payment_card_guard import reject_card_leak
reject_card_leak(open(sys.argv[1], "rb").read(), context.get("payment_card"))
PY
  then
    exit 65
  fi
fi

if [[ -n "$(git -C "${state_workspace}" status --porcelain)" ]]; then
  git -C "${state_workspace}" add -- "${output_paths[@]}"
  git -C "${state_workspace}" commit -m "Save codex.procedure result" >/dev/null
fi
commit_sha="$(git -C "${state_workspace}" rev-parse HEAD)"

GIT_TERMINAL_PROMPT=0 \
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=http.extraHeader \
GIT_CONFIG_VALUE_0="${TIN_EPHEMERAL_AUTH_HEADER}" \
  git -C "${state_workspace}" push ephemeral "HEAD:refs/heads/${TIN_EPHEMERAL_BRANCH}" >/dev/null

printf 'TIN_PROCEDURE_COMMIT_SHA=%s\n' "${commit_sha}"
base64 -w 0 "${result_file}" | sed 's/^/TIN_PROCEDURE_RESULT=/'
printf '\n'
