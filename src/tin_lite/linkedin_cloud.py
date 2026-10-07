"""Dedicated trusted E2B transport. It never selects an ordinary workflow image."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from e2b import AsyncSandbox, SandboxNotFoundException

from tin_lite.connection_collection import CollectionError, Person, canonical_json
from tin_lite.usage_capture import observe_sandbox


class LinkedInCloud:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings
        credential = getattr(settings, "linkedin_e2b_api_key", None)
        self.key = credential.get_secret_value() if credential is not None else None

    async def cleanup(self, sandbox_id):
        if self.key is None:
            raise CollectionError("cloud_unavailable")
        try:
            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=self.key)
            await sandbox.kill()
            await observe_sandbox(self.db, sandbox_id, ended=True)
        except SandboxNotFoundException:
            await observe_sandbox(self.db, sandbox_id, absent=True)

    async def page(self, job, session, source):
        """One receipted attempt per page. Uncertain purchases are never repeated."""
        if self.key is None:
            raise CollectionError("cloud_unavailable")
        key = f"collection:{job['run_id']}:cloud:{job['friend_index']}:{job['next_page']}"
        async with self.db.effect_lock(key, "connection_cloud_v1") as (conn, receipt):
            if receipt:
                saved = receipt.result or {}
                if receipt.status == "completed":
                    return saved["response"]
                # Recover known resources but do not rerun an uncertain network request.
                if saved.get("sandbox_id"):
                    await self.cleanup(saved["sandbox_id"])
                    await self.db.pool.execute(
                        "UPDATE connection_collection_jobs SET "
                        "cloud_cleanup_confirmed=true WHERE run_id=$1",
                        job["run_id"],
                    )
                elif saved.get("cleanup_after") and datetime.fromisoformat(
                    saved["cleanup_after"]
                ) > datetime.now(UTC):
                    raise CollectionError("cloud_cleanup_pending")
                else:
                    # Unknown sandbox identity is unresolved even after the deadline: the
                    # lifetime fence prevents a new request, but deletion must be verified.
                    raise CollectionError("cloud_cleanup_pending")
                raise CollectionError("cloud_failed")
            if not job["cloud_template"]:
                raise CollectionError("cloud_unavailable")
            await self.db.start_effect(conn, execution_key=key, operation="connection_cloud_v1")
            deadline = min(job["deadline"], datetime.now(UTC) + timedelta(seconds=90))
            saved = {
                "cleanup_after": deadline.isoformat(),
                "template": job["cloud_template"],
                "run_id": str(job["run_id"]),
                "supplier_usage": "unconfirmed",
            }
            await self.db.save_effect_progress(conn, execution_key=key, result=saved)
            # Snapshot the switchboard fence before any secret reaches remote compute.
            await self.db.pool.execute(
                """UPDATE connection_collection_jobs SET
                cloud_deadline=$2,cloud_cleanup_confirmed=false WHERE run_id=$1""",
                job["run_id"],
                deadline,
            )
            sandbox = None
            try:
                sandbox = await AsyncSandbox.create(
                    job["cloud_template"],
                    timeout=max(1, int((deadline - datetime.now(UTC)).total_seconds())),
                    lifecycle={"on_timeout": "kill", "auto_resume": False},
                    metadata={
                        "run_id": str(job["run_id"]),
                        "execution_key": key,
                        "profile": "linkedin_http_v1",
                    },
                    network={
                        "allow_out": ["www.linkedin.com"],
                        "deny_out": lambda context: [context.all_traffic],
                    },
                    api_key=self.key,
                )
                saved["sandbox_id"] = sandbox.sandbox_id
                await self.db.save_effect_progress(conn, execution_key=key, result=saved)
                await self.db.pool.execute(
                    "UPDATE connection_collection_jobs SET cloud_sandbox_id=$2 WHERE run_id=$1",
                    job["run_id"],
                    sandbox.sandbox_id,
                )
                await observe_sandbox(self.db, sandbox.sandbox_id, info=await sandbox.get_info())
                packet = {
                    "session": session,
                    "source": source,
                    "keywords": job["inputs"]["keywords"],
                    "page": job["next_page"],
                    "expires_at": deadline.timestamp(),
                }
                # Static command and a root-only file; credentials are never shell arguments,
                # environment variables, stdout, model input, or a project artifact.
                await sandbox.files.write(
                    "/run/tin-collection/request.json", canonical_json(packet), user="root"
                )
                await sandbox.commands.run(
                    "chmod 600 /run/tin-collection/request.json && python "
                    "/opt/tin-collection/runner.py",
                    user="root",
                    timeout=50,
                )
                raw = await sandbox.files.read("/run/tin-collection/result.json", user="root")
                if not isinstance(raw, str) or len(raw.encode()) > 200_000:
                    raise CollectionError("cloud_failed")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise CollectionError("cloud_failed")
                if set(value) == {"error"}:
                    if value["error"] not in {
                        "session_expired",
                        "account_changed",
                        "access_denied",
                        "challenge",
                        "rate_limited",
                        "unsupported_search_contract",
                        "unsupported_identity",
                        "cloud_failed",
                    }:
                        raise CollectionError("cloud_failed")
                elif set(value) == {"people", "next_page"}:
                    if (
                        type(value["next_page"]) is not bool
                        or not isinstance(value["people"], list)
                        or len(value["people"]) > 10
                    ):
                        raise CollectionError("cloud_failed")
                    value["people"] = [
                        Person.model_validate(p).model_dump() for p in value["people"]
                    ]
                else:
                    raise CollectionError("cloud_failed")
                saved["response"] = {**value, "observed_at": datetime.now(UTC).isoformat()}
            except CollectionError:
                raise
            except Exception:
                raise CollectionError("cloud_failed") from None
            finally:
                if sandbox is not None:
                    # Cleanup failure is intentionally fatal to handoff. A local attempt
                    # may start only once the same resource has been confirmed gone.
                    try:
                        async with asyncio.timeout(20):
                            await self.cleanup(sandbox.sandbox_id)
                        await self.db.pool.execute(
                            "UPDATE connection_collection_jobs SET "
                            "cloud_cleanup_confirmed=true WHERE run_id=$1",
                            job["run_id"],
                        )
                    except Exception:
                        raise CollectionError("cloud_cleanup_pending") from None
            saved["supplier_usage"] = "recorded_separately"
            await self.db.complete_effect(conn, execution_key=key, result=saved)
            return saved["response"]
