#!/usr/bin/env bash
# Staging's database: stock Postgres on the staging host, set up to behave like production's
# managed Postgres where Tin can tell the difference. Production's pg_strict (no DELETE or
# UPDATE without WHERE) is not available here; tests/test_sql_requires_where.py covers it.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 64
fi

major="${TIN_LITE_POSTGRES_MAJOR:-18}"
# Production's exact build when the archive still has it; otherwise the latest minor.
version="${TIN_LITE_POSTGRES_VERSION:-18.6-1.pgdg12+2}"
database=tin
role=tin_app
password_file=/etc/tin-lite/postgres-password

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg postgresql-common
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "${VERSION_CODENAME}")-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update
fi
if apt-cache madison "postgresql-${major}" | awk -F'|' '{gsub(/ /, "", $2); print $2}' |
  grep -qxF "${version}"; then
  apt-get install -y "postgresql-${major}=${version}"
  apt-mark hold "postgresql-${major}"
else
  echo "postgresql-${major} ${version} is not available; installing the latest ${major}.x" >&2
  apt-get install -y "postgresql-${major}"
fi

install -d -m 755 "/etc/postgresql/${major}/main/conf.d"
cat > "/etc/postgresql/${major}/main/conf.d/tin-staging.conf" <<'EOF'
# Production's values where they shape behavior Tin can observe.
listen_addresses = 'localhost'
max_connections = 25
ssl = on
timezone = 'UTC'
log_timezone = 'UTC'
work_mem = '2MB'
maintenance_work_mem = '16MB'
shared_buffers = '64MB'
idle_in_transaction_session_timeout = 0
random_page_cost = 1.1
effective_io_concurrency = 32
default_toast_compression = 'lz4'
EOF
systemctl enable "postgresql@${major}-main"
systemctl restart "postgresql@${major}-main"

install -d -m 750 -o root -g tinlite /etc/tin-lite 2>/dev/null || install -d -m 750 /etc/tin-lite
if [[ ! -s "${password_file}" ]]; then
  umask 077
  openssl rand -hex 32 > "${password_file}"
fi
chmod 600 "${password_file}"
password="$(cat "${password_file}")"

# Like production's application role: no superuser, cannot create databases or roles.
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${role}') THEN
    CREATE ROLE ${role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END
\$\$;
ALTER ROLE ${role} PASSWORD '${password}';
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${database}'" | grep -q 1; then
  sudo -u postgres createdb --owner "${role}" "${database}"
fi
echo "postgres ready: database ${database}, role ${role}, password in ${password_file}"
