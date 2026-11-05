#!/usr/bin/env bash
# The `tin-film` entry point inside the studio sandbox: the film kit (web/film) on the diagram
# layer's pinned Node packages and Chromium. The browser fetches the product directly, as the
# studio's capture does; the run's proxy variables are not for it.
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export PLAYWRIGHT_BROWSERS_PATH=/opt/tin-lite/diagram/browsers
exec node /opt/tin-lite/diagram/web/film/tin-film.js "$@"
