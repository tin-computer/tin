#!/usr/bin/env bash
set -euo pipefail

: "${TIN_LITE_GCP_PROJECT:?Set TIN_LITE_GCP_PROJECT to your deployment project}"

# Both targets keep their release env on the host (/etc/tin-lite/release.env) and run
# migrations before the release goes live. TIN_LITE_UPLOAD_ENV=1 uploads a local .env
# instead, for a host that has not been given one yet.
target="${TIN_LITE_DEPLOY_TARGET:-prod}"
case "${target}" in
  prod)
    instance=tin-lite-switchboard
    address=tin-lite-switchboard-ip
    default_host=app.tin.computer
    ;;
  staging)
    instance=tin-lite-staging
    address=tin-lite-staging-ip
    default_host=app.staging.tin.computer
    ;;
  *)
    echo "TIN_LITE_DEPLOY_TARGET must be prod or staging" >&2
    exit 64
    ;;
esac
zone=us-central1-a
region=us-central1
upload_env="${TIN_LITE_UPLOAD_ENV:-0}"
run_migrations="${TIN_LITE_RUN_MIGRATIONS:-1}"
for flag in "${upload_env}" "${run_migrations}" "${TIN_LITE_FORCE_HOST_INSTALL:-0}"; do
  if [[ "${flag}" != 0 && "${flag}" != 1 ]]; then
    echo "TIN_LITE_UPLOAD_ENV, TIN_LITE_RUN_MIGRATIONS and TIN_LITE_FORCE_HOST_INSTALL are 0 or 1" >&2
    exit 64
  fi
done

root="$(git rev-parse --show-toplevel)"
cd "${root}"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "tracked worktree changes must be committed before deployment" >&2
  exit 65
fi
github_private_key_path=""
if [[ "${upload_env}" == "1" ]]; then
  if [[ ! -f .env ]]; then
    echo "local .env is required" >&2
    exit 66
  fi
  if grep -q '^OPENAI_API_KEY=' .env; then
    echo "OPENAI_API_KEY is forbidden" >&2
    exit 67
  fi
  github_private_key_path="$(sed -n 's/^TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=//p' .env | tail -n 1)"
fi
if [[ -n "${github_private_key_path}" ]]; then
  if [[ ! -f "${github_private_key_path}" ]]; then
    echo "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH must name a readable local file" >&2
    exit 68
  fi
  if ! openssl pkey -in "${github_private_key_path}" -check -noout >/dev/null 2>&1; then
    echo "TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH is not a valid private key" >&2
    exit 68
  fi
fi

release_sha="$(git rev-parse HEAD)"
temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
git archive --format=tar.gz --output="${temporary_dir}/tin-lite-release.tar.gz" HEAD

# Each deploy uploads into its own private directory, so deploys by different people (or
# by CI) never collide on files another account left in /tmp. A first connection may print
# key-generation output, so only the directory line is kept.
upload_dir="$(gcloud compute ssh "${instance}" \
  --project "${TIN_LITE_GCP_PROJECT}" --zone "${zone}" --quiet \
  --command='mktemp -d /tmp/tin-deploy.XXXXXXXXXX' |
  tr -d '\r' | grep -E '^/tmp/tin-deploy\.[A-Za-z0-9]{10}$' | tail -n 1 || true)"
if [[ ! "${upload_dir}" =~ ^/tmp/tin-deploy\.[A-Za-z0-9]{10}$ ]]; then
  echo "could not create a private upload directory on ${instance}" >&2
  exit 70
fi

release_files=("${temporary_dir}/tin-lite-release.tar.gz")
if [[ "${upload_env}" == "1" ]]; then
  release_files+=(.env)
fi
gcloud compute scp \
  "${release_files[@]}" \
  infra/install_switchboard.sh \
  infra/install_forward_proxy.sh \
  infra/proxy_policy.py \
  src/tin_lite/proxy_grants.py \
  infra/activate_release.sh \
  "${instance}:${upload_dir}/" \
  --project "${TIN_LITE_GCP_PROJECT}" --zone "${zone}" --quiet

if [[ -n "${github_private_key_path}" ]]; then
  gcloud compute scp \
    "${github_private_key_path}" \
    "${instance}:${upload_dir}/tin-lite-github-app.pem" \
    --project "${TIN_LITE_GCP_PROJECT}" --zone "${zone}" --quiet
  gcloud compute ssh "${instance}" \
    --project "${TIN_LITE_GCP_PROJECT}" --zone "${zone}" --quiet \
    --command="sudo chown root:root ${upload_dir}/tin-lite-github-app.pem && \
sudo chmod 600 ${upload_dir}/tin-lite-github-app.pem"
fi

static_ip="${TIN_LITE_STATIC_IP:-}"
if [[ -z "${static_ip}" ]]; then
  static_ip="$(gcloud compute addresses describe "${address}" \
    --project "${TIN_LITE_GCP_PROJECT}" --region "${region}" --format='value(address)')"
fi
if [[ ! "${static_ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "the ${target} static IP is not an IPv4 address" >&2
  exit 64
fi
product_host="${TIN_LITE_PRODUCT_HOST:-${default_host}}"
if [[ ! "${product_host}" =~ ^[a-zA-Z0-9.-]+$ ]]; then
  echo "TIN_LITE_PRODUCT_HOST must be a DNS hostname" >&2
  exit 64
fi
move_env=""
if [[ "${upload_env}" == "1" ]]; then
  move_env="mv ${upload_dir}/.env ${upload_dir}/tin-lite.env && "
fi
force_install="${TIN_LITE_FORCE_HOST_INSTALL:-0}"
# activate_release.sh takes the host lock, installs host setup only when it changed,
# migrates, switches, and waits for the new process to report startup.
gcloud compute ssh "${instance}" \
  --project "${TIN_LITE_GCP_PROJECT}" --zone "${zone}" --quiet \
  --command="${move_env}sudo env TIN_LITE_STATIC_IP=${static_ip} TIN_LITE_PRODUCT_HOST=${product_host} \
TIN_LITE_UPLOAD_DIR=${upload_dir} TIN_LITE_RUN_MIGRATIONS=${run_migrations} \
TIN_LITE_FORCE_HOST_INSTALL=${force_install} bash ${upload_dir}/activate_release.sh ${release_sha}; \
status=\$?; sudo rm -rf ${upload_dir}; exit \$status"

# The new process has reported startup; these confirm it serves through Caddy.
public_host="tin-lite-switchboard.${static_ip//./-}.sslip.io"
curl --http1.1 --fail --silent --show-error --retry 20 --retry-all-errors --retry-delay 3 \
  "https://${public_host}/healthz"

curl --http1.1 --fail --silent --show-error --retry 20 --retry-all-errors --retry-delay 3 \
  "https://${product_host}/healthz"
