#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 64
fi
if [[ -z "${TIN_LITE_STATIC_IP:-}" ]]; then
  echo "TIN_LITE_STATIC_IP is required" >&2
  exit 64
fi

# Drain sandbox work before this migration. Close the old plaintext listener
# first; an incomplete install must not leave the vulnerable proxy available.
if systemctl cat tinyproxy.service >/dev/null 2>&1; then
  systemctl disable --now tinyproxy.service
  systemctl mask tinyproxy.service
fi
# Prevent the distribution package from starting an unfenced default listener.
systemctl mask squid.service

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl debian-archive-keyring debian-keyring git gnupg \
  openssl squid-openssl nftables certbot python3

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
    gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    -o /etc/apt/sources.list.d/caddy-stable.list
  chmod 644 /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv python install 3.12

if ! id tinlite >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/tin-lite --shell /usr/sbin/nologin tinlite
fi
install -d -m 750 -o tinlite -g tinlite /opt/tin-lite /opt/tin-lite/releases
install -d -m 750 -o tinlite -g tinlite /var/cache/tin-lite-uv
install -d -m 750 -o root -g tinlite /etc/tin-lite
install -d -m 750 -o caddy -g caddy /var/log/caddy

install -d -m 755 /var/lib/tin-lite-proxy-acme

technical_host="tin-lite-switchboard.${TIN_LITE_STATIC_IP//./-}.sslip.io"
product_host="${TIN_LITE_PRODUCT_HOST:-app.tin.computer}"
proxy_host="tin-lite-proxy.${TIN_LITE_STATIC_IP//./-}.sslip.io"
# The browser app may move without moving MCP, callbacks or existing sandboxes.
# Preserve the operator's app setting on subsequent installs.
app_url="${TIN_LITE_APP_URL:-}"
if [[ -z "${app_url}" && -f /etc/tin-lite/runtime.env ]]; then
  app_url="$(sed -n 's/^TIN_LITE_APP_URL=//p' /etc/tin-lite/runtime.env | tail -n 1)"
fi
app_url="${app_url%/}"
if [[ -n "${app_url}" && ! "${app_url}" =~ ^https://[a-zA-Z0-9.-]+$ ]]; then
  echo "TIN_LITE_APP_URL must be an HTTPS origin with a DNS hostname" >&2
  exit 64
fi
public_hosts="${product_host}"
if [[ -n "${app_url}" && "${app_url}" != "https://${product_host}" ]]; then
  public_hosts="${product_host}, ${app_url#https://}"
fi
legacy_url="${TIN_LITE_LEGACY_PUBLIC_URL:-}"
if [[ -z "${legacy_url}" && -f /etc/tin-lite/runtime.env ]]; then
  legacy_url="$(sed -n 's/^TIN_LITE_LEGACY_PUBLIC_URL=//p' /etc/tin-lite/runtime.env | tail -n 1)"
fi
legacy_url="${legacy_url%/}"
if [[ -n "${legacy_url}" && ! "${legacy_url}" =~ ^https://[a-zA-Z0-9.-]+$ ]]; then
  echo "TIN_LITE_LEGACY_PUBLIC_URL must be an HTTPS origin with a DNS hostname" >&2
  exit 64
fi
if [[ -n "${legacy_url}" && "${legacy_url}" != "https://${product_host}" && "${legacy_url}" != "${app_url}" ]]; then
  public_hosts="${public_hosts}, ${legacy_url#https://}"
fi
cat > /etc/caddy/Caddyfile <<EOF
http://${proxy_host} {
    handle /.well-known/acme-challenge/* {
        root * /var/lib/tin-lite-proxy-acme
        file_server
    }
    handle {
        respond 404
    }
}

${technical_host} {
    @health path /healthz
    handle @health {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        redir https://${product_host}{uri} permanent
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
    }
}

${public_hosts} {
    encode zstd gzip
    # Webhooks (RB2B, Stripe, GitHub, Twilio) go to the webhook intake first, a web-only
    # process a deploy restarts only once the main one is back, and to the main process
    # while the intake restarts: a sender always gets an answer.
    @webhooks path /webhooks/*
    handle @webhooks {
        reverse_proxy 127.0.0.1:8001 127.0.0.1:8000 {
            lb_policy first
            lb_try_duration 10s
            lb_try_interval 250ms
            fail_duration 10s
        }
    }
    handle {
        reverse_proxy 127.0.0.1:8000
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
    }
    log {
        output file /var/log/caddy/tin-lite-access.log
        format json
    }
}
EOF
caddy validate --config /etc/caddy/Caddyfile
touch /var/log/caddy/tin-lite-access.log
chown caddy:caddy /var/log/caddy/tin-lite-access.log
chmod 640 /var/log/caddy/tin-lite-access.log

# The template owns these keys; operator-added lines (billing, Stripe, pilot templates,
# private-workflow projects) are preserved so a redeploy cannot silently drop them.
runtime_env=/etc/tin-lite/runtime.env
runtime_env_next="$(mktemp /etc/tin-lite/.runtime.env.XXXXXX)"
authorized_parties="https://${product_host},http://127.0.0.1:8000,http://localhost:8000"
if [[ -f "${runtime_env}" ]]; then
  existing_parties="$(sed -n 's/^CLERK_AUTHORIZED_PARTIES=//p' "${runtime_env}" | tail -n 1)"
  if [[ -n "${existing_parties}" ]]; then
    authorized_parties="${existing_parties},${authorized_parties}"
  fi
fi
if [[ -n "${app_url:-}" ]]; then
  authorized_parties="${authorized_parties},${app_url}"
fi
if [[ -n "${legacy_url:-}" ]]; then
  authorized_parties="${authorized_parties},${legacy_url}"
fi
proxy_host="tin-lite-proxy.${TIN_LITE_STATIC_IP//./-}.sslip.io"
egress_hosts="${TIN_LITE_STATIC_IP},${technical_host},${product_host},${proxy_host}"
if [[ -n "${legacy_url:-}" ]]; then
  egress_hosts="${egress_hosts},${legacy_url#https://}"
fi
authorized_parties="$(printf '%s' "${authorized_parties}" | awk -v RS=, '!seen[$0]++ {printf "%s%s", sep, $0; sep=","}')"
cat > "${runtime_env_next}" <<EOF
TIN_LITE_PUBLIC_URL=https://${product_host}
CLERK_AUTHORIZED_PARTIES=${authorized_parties}
TIN_LITE_PROXY_URL=https://${proxy_host}:8888
TIN_LITE_PROXY_GRANT_DIR=/var/lib/tin-lite-proxy-grants
TIN_LITE_EGRESS_ALLOW_HOSTS=${egress_hosts}
UV_CACHE_DIR=/var/cache/tin-lite-uv
EOF
if [[ -n "${app_url:-}" ]]; then
  printf '%s\n' "TIN_LITE_APP_URL=${app_url}" >> "${runtime_env_next}"
fi
if [[ -n "${legacy_url:-}" ]]; then
  printf '%s\n' "TIN_LITE_LEGACY_PUBLIC_URL=${legacy_url}" >> "${runtime_env_next}"
fi
if [[ -f "${runtime_env}" ]]; then
  while IFS= read -r line; do
    key="${line%%=*}"
    case "${key}" in TIN_LITE_AUTH_JSON_PATHS|TIN_LITE_BROKER_GRANT_TTL|TIN_LITE_CODEX_AUTH_JSON) continue ;; esac
    if [[ -n "${key}" && "${key}" != \#* && "${line}" == *=* ]] \
      && ! grep -q "^${key}=" "${runtime_env_next}"; then
      printf '%s\n' "${line}" >> "${runtime_env_next}"
    fi
  done < "${runtime_env}"
  cp -p "${runtime_env}" "${runtime_env}.before-$(date -u +%Y%m%dT%H%M%SZ)"
fi
# Sandbox selection is operator-owned, just like isolated/browser profiles. Only
# supply the bootstrap default when no selection has been configured yet.
if ! grep -q '^TIN_LITE_E2B_TEMPLATE=' "${runtime_env_next}"; then
  printf '%s\n' 'TIN_LITE_E2B_TEMPLATE=tin-lite-codex' >> "${runtime_env_next}"
fi
mv "${runtime_env_next}" "${runtime_env}"
chmod 640 /etc/tin-lite/runtime.env
chown root:tinlite /etc/tin-lite/runtime.env

cat > /etc/systemd/system/tin-lite-switchboard.service <<'EOF'
[Unit]
Description=Tin Lite API and Temporal worker
After=network-online.target tin-lite-proxy.service caddy.service
Wants=network-online.target

[Service]
Type=simple
User=tinlite
Group=tinlite
WorkingDirectory=/opt/tin-lite/current
EnvironmentFile=/etc/tin-lite/clerk.env
EnvironmentFile=/etc/tin-lite/runtime.env
ExecStartPre=/usr/bin/find /var/lib/tin-lite-proxy-grants -type f -delete
ExecStart=/usr/local/bin/uv run --frozen --no-dev tin-lite serve
Restart=always
RestartSec=5
# SIGTERM drains the Temporal worker (TIN_LITE_WORKER_GRACEFUL_SHUTDOWN_SECONDS,
# default 300s) while HTTP keeps serving, then HTTP gets 20s. Stay above both.
TimeoutStopSec=420
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/cache/tin-lite-uv /var/lib/tin-lite-proxy-grants

[Install]
WantedBy=multi-user.target
EOF

# The webhook intake: the same release, web-only (no Temporal worker, no background loops),
# on its own port with two database connections. activate_release.sh restarts it after the
# main process is up.
cat > /etc/systemd/system/tin-lite-intake.service <<'UNIT'
[Unit]
Description=Tin Lite webhook intake (web-only)
After=network-online.target caddy.service
Wants=network-online.target

[Service]
Type=simple
User=tinlite
Group=tinlite
WorkingDirectory=/opt/tin-lite/current
EnvironmentFile=/etc/tin-lite/clerk.env
EnvironmentFile=/etc/tin-lite/runtime.env
Environment=TIN_LITE_WEB_ONLY=true
Environment=TIN_LITE_PORT=8001
Environment=TIN_LITE_DB_POOL_MAX=2
ExecStart=/usr/local/bin/uv run --frozen --no-dev tin-lite serve
Restart=always
RestartSec=5
TimeoutStopSec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/cache/tin-lite-uv

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable tin-lite-intake
systemctl enable caddy
systemctl restart caddy
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bash "${source_dir}/install_forward_proxy.sh"
# Old values may remain in root-owned runtime backups, but no service accepts them.
rm -f /etc/tin-lite/proxy-password /etc/tinyproxy/tinyproxy.conf
