#!/usr/bin/env bash
# Activate an uploaded release on this host. deploy.sh uploads everything into a private
# per-deploy directory and runs this script as root; everything below happens under one host
# lock, so deploys by people and by CI never interleave.
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

exec 9>/run/lock/tin-lite-deploy.lock
if ! flock -w 1800 9; then
  echo "another deploy holds the host lock" >&2
  exit 75
fi

# The root disk has run out of inodes before (hundreds of releases); stop before it does.
inode_use="$(df --output=ipcent /opt/tin-lite | tail -n 1 | tr -dc '0-9')"
if (( inode_use >= 90 )); then
  echo "inode use on /opt/tin-lite is ${inode_use}%; prune releases before deploying" >&2
  exit 74
fi

# Host setup (Caddy, forward proxy, units) runs only when its scripts changed: it restarts
# the proxy that in-flight sandboxes use. TIN_LITE_FORCE_HOST_INSTALL=1 forces it.
if [[ -f "${upload_dir}/install_switchboard.sh" ]]; then
  infra_hash="$(cd "${upload_dir}" && cat install_switchboard.sh install_forward_proxy.sh \
    proxy_policy.py proxy_grants.py | sha256sum | cut -d' ' -f1)"
  if [[ "${TIN_LITE_FORCE_HOST_INSTALL:-0}" == "1" ]] ||
    [[ "$(cat /etc/tin-lite/host-install.sha256 2>/dev/null)" != "${infra_hash}" ]]; then
    echo "host setup changed; installing"
    bash "${upload_dir}/install_switchboard.sh"
    printf '%s\n' "${infra_hash}" > /etc/tin-lite/host-install.sha256
  fi
fi

release_sha="$1"
release_dir="/opt/tin-lite/releases/${release_sha}"
previous="$(readlink /opt/tin-lite/current || true)"
echo "current release: ${previous:-none}"
install -d -m 750 -o tinlite -g tinlite "${release_dir}"
tar -xzf "${upload_dir}/tin-lite-release.tar.gz" -C "${release_dir}"
if [[ -f "${upload_dir}/tin-lite-github-app.pem" ]]; then
  install -m 640 -o root -g tinlite \
    "${upload_dir}/tin-lite-github-app.pem" /etc/tin-lite/github-app.pem
  sed -i \
    's#^TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=.*#TIN_LITE_GITHUB_APP_PRIVATE_KEY_PATH=/etc/tin-lite/github-app.pem#' \
    "${upload_dir}/tin-lite.env"
fi
# An uploaded .env wins; otherwise the release env kept on the host is used.
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
restarts_before="$(systemctl show -p NRestarts --value tin-lite-switchboard)"
restarted_at="$(date '+%Y-%m-%d %H:%M:%S')"
# A restart drains the Temporal worker first (up to the unit's stop timeout).
systemctl restart tin-lite-switchboard

rm -f "${upload_dir}/tin-lite-release.tar.gz" "${upload_dir}/tin-lite.env"

# /healthz stays 200 from the old process while it drains, so judge the new one by its own
# startup line and the restart counter. A failure stops here and is reported; nothing is
# rolled back automatically, because a release may have changed the catalog for good.
deadline=$((SECONDS + ${TIN_LITE_STARTUP_TIMEOUT:-420}))
until journalctl -u tin-lite-switchboard --since "${restarted_at}" --no-pager -q |
  grep -q "Application startup complete"; do
  if [[ "$(systemctl show -p NRestarts --value tin-lite-switchboard)" != "${restarts_before}" ]]; then
    echo "the new release is restarting; it did not start cleanly (previous: ${previous})" >&2
    journalctl -u tin-lite-switchboard --since "${restarted_at}" --no-pager -q | tail -n 40 >&2
    exit 71
  fi
  if (( SECONDS > deadline )); then
    echo "the new release did not report startup in time (previous: ${previous})" >&2
    journalctl -u tin-lite-switchboard --since "${restarted_at}" --no-pager -q | tail -n 40 >&2
    exit 72
  fi
  sleep 5
done
echo "release ${release_sha} started"

# Keep the newest 20 releases, plus whatever current and the previous release point at.
keep_current="$(readlink -f /opt/tin-lite/current)"
keep_previous="$(readlink -f "${previous:-/nonexistent}" 2>/dev/null || true)"
ls -1dt /opt/tin-lite/releases/*/ 2>/dev/null | tail -n +21 | while read -r old; do
  old="${old%/}"
  if [[ "${old}" != "${keep_current}" && "${old}" != "${keep_previous}" ]]; then
    rm -rf --one-file-system "${old}"
  fi
done
