"""Trusted, fixed read-only program. No agent or user-authored code runs in this image."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/tin-collection")
from tin_lite.connection_collection import CollectionError
from tin_lite.linkedin_session import read_page

signal.alarm(45)
request = Path("/run/tin-collection/request.json")
result = {"error": "cloud_failed"}
try:
    raw = request.read_bytes()
    request.unlink()
    if len(raw) > 200_000:
        raise ValueError
    packet = json.loads(raw)
    if not time.time() < packet["expires_at"] <= time.time() + 90:
        raise ValueError
    signal.alarm(max(1, min(45, int(packet["expires_at"] - time.time()))))
    result = asyncio.run(
        read_page(packet["session"], packet["source"], packet["keywords"], packet["page"])
    )
except CollectionError as exc:
    result = {"error": exc.code}
except Exception:
    result = {"error": "cloud_failed"}
finally:
    request.unlink(missing_ok=True)
    destination = Path("/run/tin-collection/result.json")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(result, stream, separators=(",", ":"))
