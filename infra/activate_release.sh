#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 64
fi
if [[ ! "${1:-}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "a full git SHA is required" >&2
  exit 64
fi

# deploy.sh uploads into a private per-deploy directory; /tmp is the older layout.
upload_dir="${TIN_LITE_UPLOAD_DIR:-/tmp}"
if [[ "${upload_dir}" != /tmp && ! "${upload_dir}" =~ ^/tmp/tin-deploy\.[A-Za-z0-9]{10}$ ]]; then
  echo "TIN_LITE_UPLOAD_DIR must be /tmp or a deploy directory under it" >&2
  exit 64
fi

cleanup() {
  rm -f "${upload_dir}/tin-lite-github-app.pem"
}
trap cleanup EXIT

release_sha="$1"
release_dir="/opt/tin-lite/releases/${release_sha}"
install -d -m 750 -o tinlite -g tinlite "${release_dir}"
tar -xzf "${upload_dir}/tin-lite-release.tar.gz" -C "${release_dir}"
if [[ -f "${upload_dir}/tin-lite-github-app.pem" ]]; then
  install -m 640 -o root -g tinlite \
    "${upload_dir}/tin-lite-github-app.pem" /etc/tin-lite/github-app.pem
  sed -i \
    's#^TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=.*#TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=/etc/tin-lite/github-app.pem#' \
    "${upload_dir}/tin-lite.env"
fi
# An uploaded .env wins; otherwise the release env kept on the host (staging) is used.
release_env="${upload_dir}/tin-lite.env"
if [[ ! -f "${release_env}" ]]; then
  release_env=/etc/tin-lite/release.env
fi
if [[ ! -f "${release_env}" ]]; then
  echo "no release env: upload .env or keep /etc/tin-lite/release.env on the host" >&2
  exit 66
fi
install -m 640 -o tinlite -g tinlite "${release_env}" "${release_dir}/.env"
chown -R tinlite:tinlite "${release_dir}"

sudo -u tinlite env UV_CACHE_DIR=/var/cache/tin-lite-uv \
  /usr/local/bin/uv sync --frozen --no-dev --directory "${release_dir}"

# Migrations run from the new release, under the service env, before it goes live.
if [[ "${TIN_LITE_RUN_MIGRATIONS:-0}" == "1" ]]; then
  systemd-run --wait --pipe --collect -p User=tinlite -p WorkingDirectory="${release_dir}" \
    -p EnvironmentFile=/etc/tin-lite/clerk.env -p EnvironmentFile=/etc/tin-lite/runtime.env \
    -E UV_CACHE_DIR=/var/cache/tin-lite-uv \
    /usr/local/bin/uv run --frozen --no-dev tin-lite migrate
fi

ln -sfn "${release_dir}" /opt/tin-lite/current
systemctl enable tin-lite-switchboard
systemctl restart tin-lite-switchboard

rm -f "${upload_dir}/tin-lite-release.tar.gz" "${upload_dir}/tin-lite.env"
