"""Operator spike for warm reach: run linkedin_spike_probe with a project's saved session.

`tin-lite linkedin-spike` runs on a Tin host with the service's settings. It opens the
project's saved LinkedIn session (network.linkedin) the way the cloud collector does and
runs the probe either from this host, one steady address, or inside the LinkedIn sandbox
image, which leaves from E2B's addresses. It prints the probe's report: statuses, counts and
LinkedIn type names, never names, text or session values. Not product code.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from tin_lite.connection_collection import canonical_json, cloud_credential
from tin_lite.db import Database
from tin_lite.integrations import CredentialCipher
from tin_lite.linkedin_spike_probe import probe

PROBE = Path(__file__).with_name("linkedin_spike_probe.py")


async def saved_session(database, settings, project_id: UUID) -> dict:
    row = await database.pool.fetchrow(
        "SELECT credential_ciphertext FROM integration_connections "
        "WHERE project_id=$1 AND provider_key='network.linkedin' AND status='connected'",
        project_id,
    )
    if not row or not row["credential_ciphertext"]:
        raise SystemExit("No saved LinkedIn session: connect with cloud reading on this project.")
    cipher = CredentialCipher(settings.integration_credential_key.get_secret_value())
    sealed = json.loads(
        cipher.decrypt(
            row["credential_ciphertext"], context=f"credential:{project_id}:network.linkedin"
        )
    )
    if datetime.fromisoformat(sealed["expires_at"]) <= datetime.now(UTC):
        raise SystemExit("The saved LinkedIn session has expired: open Chrome to refresh it.")
    return sealed["session"]


async def in_sandbox(settings, session, plan) -> dict:
    from e2b import AsyncSandbox

    sandbox = await AsyncSandbox.create(
        settings.linkedin_cloud_template,
        timeout=300,
        lifecycle={"on_timeout": "kill", "auto_resume": False},
        metadata={"profile": "linkedin_spike"},
        network={
            "allow_out": ["www.linkedin.com"],
            "deny_out": lambda context: [context.all_traffic],
        },
        api_key=cloud_credential(settings).get_secret_value(),
    )
    try:
        # The session goes in a root-only file, never a shell argument or environment value.
        await sandbox.files.write("/run/tin-spike/probe.py", PROBE.read_text(), user="root")
        await sandbox.files.write(
            "/run/tin-spike/request.json",
            canonical_json({"session": session, "plan": plan}),
            user="root",
        )
        await sandbox.commands.run(
            "chmod 600 /run/tin-spike/request.json && python /run/tin-spike/probe.py "
            "/run/tin-spike && rm -f /run/tin-spike/request.json",
            user="root",
            timeout=240,
        )
        return json.loads(await sandbox.files.read("/run/tin-spike/result.json", user="root"))
    finally:
        await sandbox.kill()


async def run(project_id: UUID, *, where: str, plan: dict) -> None:
    from tin_lite.settings import get_settings

    settings = get_settings()
    database = Database(settings.runtime_dsn)
    await database.connect()
    try:
        session = await saved_session(database, settings, project_id)
    finally:
        await database.close()
    plan = {**plan, "where": where}
    report = (
        await in_sandbox(settings, session, plan) if where == "e2b" else await probe(session, plan)
    )
    print(json.dumps(report, indent=2))


def command(args) -> None:
    plan = {"profiles": args.profile or [], "gap": args.gap}
    if args.post:
        plan["post"] = args.post
    if args.invite:
        plan["invite"] = args.invite
        plan["invite_confirm"] = args.confirm_invite
    asyncio.run(run(args.project_id, where=args.where, plan=plan))
