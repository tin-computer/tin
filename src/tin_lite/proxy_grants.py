"""Short-lived, local forward-proxy grants; also Squid's stdlib-only auth helper.

Only the switchboard can write the directory. Squid can read it. No provider or
broker authority is conferred by a proxy grant, and no reusable password is sent
to a sandbox. A worker restart removes any orphaned grants before accepting work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

# The longest procedure sandbox (procedures.MAX_SANDBOX_TIMEOUT_SECONDS, 7,200 s) plus 60 s.
# The forward proxy host runs this file as its auth helper; reinstall it there too.
MAX_TTL_SECONDS = 7260
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def validate_proxy_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TIN_LITE_PROXY_URL must be an HTTPS proxy origin without credentials")
    # Force invalid ports to fail during configuration, before issuing a grant.
    _ = parsed.port


@contextmanager
def proxy_grant(
    *, directory: Path, proxy_url: str, execution_key: str, sandbox_id: str, ttl_seconds: int
) -> Iterator[str]:
    validate_proxy_url(proxy_url)
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"proxy grant lifetime must be between 1 and {MAX_TTL_SECONDS} seconds")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    path = directory / digest
    # O_EXCL prevents replacing another grant; the setgid directory supplies the
    # proxy-readable group. No cleartext credential is persisted.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o640)
            json.dump(
                {
                    "execution_key": execution_key,
                    "sandbox_id": sandbox_id,
                    "expires_at": int(time.time()) + ttl_seconds,
                },
                handle,
            )
        parsed = urlsplit(proxy_url)
        yield urlunsplit(parsed._replace(netloc=f"tin-run:{token}@{parsed.netloc}"))
    finally:
        path.unlink(missing_ok=True)


def authorized(directory: Path, username: str, token: str) -> bool:
    if username != "tin-run" or TOKEN_PATTERN.fullmatch(token) is None:
        return False
    path = directory / hashlib.sha256(token.encode()).hexdigest()
    try:
        with path.open("rb") as handle:
            grant = json.loads(handle.read(2048))
        expires = grant["expires_at"]
        return (
            type(expires) is int
            and time.time() < expires <= time.time() + MAX_TTL_SECONDS
            and bool(grant["execution_key"])
            and bool(grant["sandbox_id"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> None:
    directory = Path(sys.argv[1])
    # Squid percent-escapes both fields. Never echo requests, credentials or file
    # contents, including on parse/authentication failures.
    for line in sys.stdin:
        fields = line.strip().split(" ") if len(line) <= 1024 else []
        valid = len(fields) == 2 and authorized(directory, *map(unquote, fields))
        print("OK" if valid else "ERR", flush=True)


if __name__ == "__main__":
    main()
