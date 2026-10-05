"""Failed-payment recovery activities: receipted Stripe and mailbox reads, one drafted batch of
emails, one founder approval, then one receipted Gmail send per still-unpaid invoice. Only the
run identifier crosses Temporal; every provider result lives in an effect receipt, so a retry
replays it and an unconfirmed send is never repeated.

Each invoice's send receipt is keyed by project and invoice, not by run, so Tin never emails a
customer about the same invoice twice, even from a later run.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tin_lite import payment_recovery
from tin_lite.content_plan_sources import positioning_files
from tin_lite.integrations import (
    GOOGLE_WORKSPACE_PROVIDER,
    STRIPE_PROVIDER,
    IntegrationAuthorizationError,
    IntegrationDeliveryRefusedError,
    IntegrationDeliveryUnknownError,
    IntegrationError,
)
from tin_lite.model_providers import (
    MessageRole,
    ModelMessage,
    ModelProviderError,
    ModelRequest,
    ReasoningEffort,
)
from tin_lite.model_usage import model_usage_scope
from tin_lite.organic_audit import digest
from tin_lite.organic_audit_publication import publish_artifacts
from tin_lite.payment_recovery import (
    KEY,
    PLAN_DOCS,
    POLICY,
    RESULT_DOCS,
    RecoveryError,
    paths,
)
from tin_lite.stripe_connection import StripeAuthenticationFailed, StripePermissionDenied
from tin_lite.usage_capture import external_usage_scope
from tin_lite.writing_style import STYLE_PATH

STEP_TOTAL = 5
# Stripe answered and will not let this key read: the founder has to fix the key.
STRIPE_STOPS = (IntegrationAuthorizationError, StripeAuthenticationFailed, StripePermissionDenied)
STRIPE_READS = ("invoices.read", "subscriptions.read", "charges.read", "prices.read")
GMAIL = ("gmail.messages.send", "gmail.messages.read")
# The provider's own wait for the drafting step, just inside the step's budget.
MODEL_TIMEOUT_SECONDS = 225


class MailboxStopped(IntegrationError):
    """Gmail refused every further send for now; the run stops and says how many went out."""


class PaymentRecoveryActivities:
    def __init__(self, *, database, storage, settings, router=None, integrations=None, sleep=None):
        self.db, self.storage, self.settings = database, storage, settings
        self.router, self.integrations = router, integrations
        self.sleep = sleep or asyncio.sleep

    # ------------------------------------------------------------ receipts

    @staticmethod
    def key(run_id: str, stage: str) -> str:
        return f"{payment_recovery.PREFIX}:{UUID(str(run_id))}:{stage}"

    @staticmethod
    def invoice_key(project_id, invoice_id: str) -> str:
        return f"{payment_recovery.PREFIX}:{UUID(str(project_id))}:invoice:{invoice_id}"

    async def _active(self, run_id: str, *, conn=None):
        run = await self.db.get_run(UUID(str(run_id)), conn=conn)
        if (
            run is None
            or run.executor != KEY
            or run.status.value not in {"pending", "running", "needs_input", "succeeded"}
        ):
            raise ApplicationError("The payment recovery is not active.", non_retryable=True)
        return run

    async def _result(self, run_id: str, stage: str):
        receipt = await self.db.get_effect(self.key(run_id, stage))
        return receipt.result if receipt and receipt.status == "completed" else None

    async def _save(self, run_id: str, stage: str, value: dict):
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_effect(conn, execution_key=key, result=value)
        return value

    async def _refuse(self, run_id: str, text: str):
        await self._save(run_id, "failure", {"detail": text})
        raise ApplicationError(text, non_retryable=True)

    async def _progress(self, run_id, step, current, summary):
        await self.db.project_run_progress(
            run_id=UUID(str(run_id)),
            mode="steps",
            step=step,
            current=current,
            total=STEP_TOTAL,
            percent=int(current * 100 / STEP_TOTAL),
            summary=summary,
        )

    async def _once(self, run_id: str, stage: str, call):
        """A read whose answer is kept, so a retried activity reuses it instead of asking again."""
        key = self.key(run_id, stage)
        receipt = await self.db.get_effect(key)
        if receipt and receipt.status == "completed":
            return receipt.result["value"]
        value = await call(key)
        await self._save(run_id, stage, {"value": value})
        return value

    # ------------------------------------------------------------ connections

    async def _stripe_connection(self, run_id: str, project_id, scope=None):
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=STRIPE_PROVIDER
        )
        granted = (connection.configuration.get("granted_capabilities") or []) if connection else []
        if connection is None or connection.status != "connected":
            await self._refuse(run_id, "Connect Stripe in Integrations, then run this again.")
        missing = [c for c in STRIPE_READS if c not in granted]
        if missing:
            await self._refuse(
                run_id,
                "Stripe's restricted key cannot read "
                + ", ".join(c.split(".")[0] for c in missing)
                + ". Add those read permissions to the key in Stripe, choose Check again in "
                "Integrations, then run this again.",
            )
        if scope and connection.external_account_id != scope["stripe"]["account_id"]:
            await self._refuse(
                run_id, "A different Stripe account is connected than this run started with."
            )
        return connection

    async def _gmail_connection(self, run_id: str, project_id):
        connection = await self.db.get_integration_connection(
            project_id=project_id, provider_key=GOOGLE_WORKSPACE_PROVIDER
        )
        granted = (connection.configuration.get("granted_capabilities") or []) if connection else []
        email = connection.configuration.get("email") if connection else None
        if (
            connection is None
            or connection.status != "connected"
            or not set(GMAIL).issubset(granted)
            or not isinstance(email, str)
        ):
            await self._refuse(
                run_id,
                "Connect Google Workspace with Gmail send in Integrations: the emails go out "
                "from your own mailbox.",
            )
        return connection

    async def _stripe_pages(self, run_id, name, operation, arguments, max_pages, connection):
        """Up to max_pages of one list; returns (records, complete)."""
        records, cursor = [], None
        for page in range(max_pages):
            args = {**arguments, "limit": 100, **({"cursor": cursor} if cursor else {})}

            async def call(key, args=args):
                return await self.integrations.stripe.call(
                    operation,
                    args,
                    connection=connection,
                    run_id=UUID(run_id),
                    execution_key=key,
                    max_response_bytes=None,
                )

            try:
                result = await self._once(run_id, f"stripe:{name}:{page + 1}", call)
            except STRIPE_STOPS as exc:
                # The key no longer reads this; anything else (a timeout, a rate limit) is
                # retried by the activity, and pages already read are kept.
                await self._refuse(run_id, f"{str(exc).rstrip('.')}. Nothing was sent.")
            records += result["records"]
            if not result.get("has_more") or not result.get("next_cursor"):
                return records, True
            cursor = result["next_cursor"]
        return records, False

    # ------------------------------------------------------------ activities

    @activity.defn(name="payment_recovery_prepare")
    async def prepare(self, run_id: str) -> None:
        """Pin the inputs, the Stripe account and the mailbox the emails are sent from."""
        if await self._result(run_id, "scope"):
            await self.db.mark_run_running(UUID(run_id))
            return
        run = await self._active(run_id)
        try:
            inputs = payment_recovery.check_inputs(dict(run.input or {}))
        except RecoveryError as exc:
            await self._refuse(run_id, str(exc))
        if self.router is None or not getattr(self.settings, "luna_api_key", None):
            await self._refuse(run_id, "This Tin deployment has no drafting model configured.")
        if self.integrations is None:
            await self._refuse(run_id, "This Tin deployment has no integrations configured.")
        stripe = await self._stripe_connection(run_id, run.project_id)
        gmail = await self._gmail_connection(run_id, run.project_id)
        project = await self.db.get_project(run.project_id)
        await self._save(
            run_id,
            "scope",
            {
                "project_id": str(run.project_id),
                "product": project.name,
                "inputs": inputs,
                "stripe": {
                    "account_id": stripe.external_account_id,
                    "livemode": stripe.configuration.get("livemode") is not False,
                },
                "gmail": {
                    "connection_id": str(gmail.id),
                    "external_account_id": gmail.external_account_id,
                    "email": gmail.configuration["email"],
                },
                "now": int(datetime.now(UTC).timestamp()),
            },
        )
        await self.db.mark_run_running(run.id)

    @activity.defn(name="payment_recovery_gather")
    async def gather(self, run_id: str) -> None:
        """Read Stripe, the mailbox and the project's own context into one pinned evidence set."""
        if await self._result(run_id, "evidence"):
            return
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        await self._progress(run_id, "gather", 1, "Reading failed payments in Stripe")
        stripe = await self._stripe_connection(run_id, run.project_id, scope)
        now = scope["now"]
        since = now - scope["inputs"]["lookback_days"] * 86_400
        window = {"created_gte": since, "created_lte": now}
        invoices, complete = await self._stripe_pages(
            run_id,
            "invoices",
            "invoices.list",
            {"status": "open", **window},
            POLICY["max_invoice_pages"],
            stripe,
        )
        partial = [] if complete else ["open invoices"]
        failed = payment_recovery.failed_invoices(invoices)
        earlier, eligible = [], []
        for customer_id in payment_recovery.candidates(invoices, len(failed)):
            reason = await self._earlier(run.project_id, failed[customer_id])
            if reason:
                newest = failed[customer_id][0]
                label = newest.get("customer_name") or newest.get("customer_email") or customer_id
                earlier.append({"customer": label, "reason": reason})
            else:
                eligible.append(customer_id)
        limit = scope["inputs"]["max_customers"]
        chosen = eligible[: limit + 10]
        subscriptions = []
        for customer_id in chosen:
            # Every status, so a canceled or paused subscription is seen and skipped.
            rows, _ = await self._stripe_pages(
                run_id,
                f"subscriptions:{customer_id}",
                "subscriptions.list",
                {"customer": customer_id, "status": "all"},
                1,
                stripe,
            )
            subscriptions += rows
        charges, complete = await self._stripe_pages(
            run_id,
            "charges",
            "charges.list",
            {"created_gte": since, "created_lte": now},
            POLICY["max_charge_pages"],
            stripe,
        )
        partial += [] if complete else ["failed charges"]
        prices, _ = await self._stripe_pages(
            run_id, "prices", "prices.list", {}, POLICY["max_price_pages"], stripe
        )
        chosen_set = set(chosen)
        cases, skipped = payment_recovery.build_cases(
            invoices=[i for i in invoices if i.get("customer") in chosen_set],
            subscriptions=subscriptions,
            charges=charges,
            prices=prices,
            now=now,
            max_customers=limit,
        )
        for customer_id in eligible[limit + 10 :]:
            newest = failed[customer_id][0]
            label = newest.get("customer_name") or newest.get("customer_email") or customer_id
            skipped.append({"customer": label, "reason": f"beyond this run's limit of {limit}"})
        await self._progress(run_id, "gather", 1, "Reading each customer's history")
        for case in cases:
            paid, _ = await self._stripe_pages(
                run_id,
                f"paid:{case['customer_id']}",
                "invoices.list",
                {"customer": case["customer_id"], "status": "paid"},
                1,
                stripe,
            )
            case["history"] = payment_recovery.history(paid)
            case["mailbox"] = await self._mailbox(run_id, run, scope, case)
            if activity.in_activity():
                activity.heartbeat({"customer": case["customer_id"]})
        context = await self._context(run.project_id)
        await self._save(
            run_id,
            "evidence",
            {
                "cases": cases,
                "skipped": skipped,
                "earlier": earlier,
                "partial": partial,
                "context": context,
                "counts": {"open_invoices": len(invoices), "charges": len(charges)},
            },
        )

    async def _earlier(self, project_id, invoices: list[dict]) -> str | None:
        """Why this customer was already handled: any of their open invoices was emailed."""
        for invoice in invoices:
            receipt = await self.db.get_effect(self.invoice_key(project_id, invoice["id"]))
            if receipt is None:
                continue
            saved = receipt.result or {}
            if receipt.status == "completed" and saved.get("status") == "sent":
                return f"already emailed about an open invoice on {saved['sent_at'][:10]}"
            return "an earlier email to them is unconfirmed; check your Sent folder"
        return None

    async def _mailbox(self, run_id, run, scope, case) -> dict:
        """The latest mail with this customer; an unreadable mailbox is a gap, not a failure."""
        gmail = scope["gmail"]
        common = {
            "project_id": run.project_id,
            "run_id": run.id,
            "connection_id": UUID(gmail["connection_id"]),
            "external_account_id": gmail["external_account_id"],
        }
        address = case["email"]

        async def read(key):
            found = await self.integrations.workspace_search_messages(
                **common,
                query=f'(from:"{address}" OR to:"{address}") newer_than:{POLICY["mailbox_days"]}d',
                max_results=10,
                execution_key=f"{key}:search",
            )
            messages = found.get("messages") or []
            thread = None
            if messages:
                thread = await self.integrations.workspace_get_thread(
                    **common, thread_id=messages[0]["thread_id"], execution_key=f"{key}:thread"
                )
            # Only these few facts are kept; the thread itself is never stored.
            return payment_recovery.mailbox(thread, customer_email=address, matches=len(messages))

        try:
            return await self._once(run_id, f"gmail:{case['customer_id']}", read)
        except IntegrationError:
            return {"recent_messages": None, "latest": None, "unavailable": True}

    async def _context(self, project_id) -> list[dict]:
        """Brand, project memory, the Start here plan, notes and the writing guide, bounded."""
        project = await self.db.get_project(project_id)
        repo = await self.storage.get_repo(project.state_repo_id)
        revision = await self.storage.head_sha(repo, project.canonical_branch)
        if revision is None:
            return []
        files = await positioning_files(storage=self.storage, project=project, revision=revision)
        context = [
            {"path": f["path"], "content": f["content"][: POLICY["context_file_bytes"]]}
            for f in files
        ]
        style = await self.storage.read_canonical_artifact_if_exists(
            repo_id=project.state_repo_id, commit_sha=revision, path=STYLE_PATH
        )
        if style:
            # First, so an oversized prompt drops context notes before the founder's voice.
            context.insert(
                0,
                {
                    "path": STYLE_PATH,
                    "content": style[: POLICY["style_bytes"]].decode("utf-8", errors="ignore"),
                },
            )
        return context

    @activity.defn(name="payment_recovery_draft")
    async def draft(self, run_id: str) -> str:
        """Draft every email in one model step, check each, and pin the exact plan."""
        if saved := await self._result(run_id, "draft"):
            return "review" if saved["emails"] else "empty"
        scope = await self._result(run_id, "scope")
        evidence = await self._result(run_id, "evidence")
        run = await self._active(run_id)
        cases = evidence["cases"]
        if not cases:
            await self._save(run_id, "draft", {"emails": [], "publication": None})
            return "empty"
        await self._progress(run_id, "draft", 2, f"Writing {len(cases)} recovery emails")
        context = list(evidence["context"])
        while True:
            user = payment_recovery.model_input(
                product=scope["product"],
                sender=scope["gmail"]["email"],
                context=context,
                cases=cases,
            )
            # Over the bound, the last context files are dropped, never a case.
            if len(user.encode()) <= POLICY["max_model_input_bytes"] or not context:
                break
            context.pop()
        parsed = await self._model(run_id, user)
        emails = payment_recovery.accept_drafts(parsed, cases, scope["product"])
        project = await self.db.get_project(run.project_id)
        documents = {
            "PLAN.md": payment_recovery.render_plan(
                sender=scope["gmail"]["email"],
                product=scope["product"],
                livemode=scope["stripe"]["livemode"],
                emails=emails,
                cases=cases,
                skipped=evidence["skipped"],
                earlier=evidence["earlier"],
                partial=evidence.get("partial") or (),
            )
        }
        publication = await self._publish(
            run_id, "plan", documents, paths(run_id, PLAN_DOCS), project
        )
        await self._save(
            run_id,
            "draft",
            {
                "emails": emails,
                "publication": publication,
            },
        )
        return "review"

    async def _model(self, run_id: str, user: str):
        """One paid drafting step. An unusable answer gives Tin's own emails, never a failure;
        an answer that never arrived is not bought twice."""
        request = ModelRequest(
            system=payment_recovery.SYSTEM,
            messages=(ModelMessage(role=MessageRole.USER, content=user),),
            output_schema=payment_recovery.DRAFT_SCHEMA,
            output_schema_name="payment_recovery_emails",
            max_output_tokens=POLICY["max_output_tokens"],
            reasoning_effort=ReasoningEffort(POLICY["reasoning_effort"]),
        )
        key, fingerprint = self.key(run_id, "model:draft"), digest(asdict(request))
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            saved = (existing.result or {}) if existing else {}
            if existing and existing.status == "completed":
                return saved.get("parsed")
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            if saved.get("attempted_at"):
                # An earlier attempt may have been charged; use Tin's emails instead of paying
                # again.
                result = {"parsed": None, "reason": "unconfirmed_previous_request"}
            else:
                await self.db.save_effect_progress(
                    conn,
                    execution_key=key,
                    result={
                        "request_sha256": fingerprint,
                        "attempted_at": datetime.now(UTC).isoformat(),
                    },
                )
                amount = POLICY["draft_reservation_usd"]
                try:
                    with (
                        model_usage_scope(run_id=run_id, step=f"{KEY}:draft", conn=conn),
                        external_usage_scope(self.db, conn, run_id, "draft", maximum_usd=amount),
                    ):
                        async with asyncio.timeout(240):
                            answer = await self.router.generate(
                                payment_recovery.ROUTE.key,
                                request,
                                timeout_seconds=MODEL_TIMEOUT_SECONDS,
                            )
                    parsed = answer.parsed if isinstance(answer.parsed, dict) else None
                    result = {"parsed": parsed, "model": answer.model}
                except ModelProviderError as exc:
                    result = {"parsed": None, "reason": str(exc)[:160] or "unusable"}
                except Exception as exc:
                    from tin_lite.billing_contracts import BillingError

                    if isinstance(exc, BillingError):
                        await self.db.complete_effect(
                            conn, execution_key=key, result={"parsed": None, "reason": exc.code}
                        )
                        raise ApplicationError(
                            "Tin credits or project spending limits do not allow drafting the "
                            "emails; nothing was sent.",
                            non_retryable=True,
                        ) from None
                    result = {"parsed": None, "reason": "provider_result_unavailable"}
            await self.db.complete_effect(
                conn, execution_key=key, result={"request_sha256": fingerprint, **result}
            )
            return result.get("parsed")

    async def _publish(self, run_id, stage, documents, names, project) -> dict:
        key = self.key(run_id, stage)
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return existing.result
            await self._active(run_id, conn=conn)
            encoded = {names[n]: c.encode() for n, c in documents.items()}
            limits = {**PLAN_DOCS, **RESULT_DOCS}
            await self.db.start_effect(conn, execution_key=key, operation=KEY)

            async def save_intent(intent):
                await self.db.save_publication_intent(conn, execution_key=key, intent=intent)

            async def validate_active():
                await self._active(run_id, conn=conn)

            async with self.db.project_state_lock(conn, project.id):
                revision = await publish_artifacts(
                    storage=self.storage,
                    repo_id=project.state_repo_id,
                    branch=project.canonical_branch,
                    documents=encoded,
                    paths=names,
                    limits={n: limits[n] for n in names},
                    message=f"{KEY} {run_id} [{key}]",
                    intent=(existing.result or {}).get("publication") if existing else None,
                    save_intent=save_intent,
                    validate_active=validate_active,
                )
            result = {"canonical_commit_sha": revision, "paths": names}
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return result

    @activity.defn(name="payment_recovery_request_review")
    async def request_review(self, run_id: str) -> None:
        draft = await self._result(run_id, "draft")
        scope = await self._result(run_id, "scope")
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        publication = draft["publication"]
        path = publication["paths"]["PLAN.md"]
        count = len(draft["emails"])
        await self._progress(run_id, "review", 3, "Waiting for your approval")
        test = "" if scope["stripe"]["livemode"] else " Stripe is in test mode, so nothing is sent."
        required = await self.db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=publication["canonical_commit_sha"],
            artifact_ref=(
                f"code.storage://{project.state_repo_id}@"
                f"{publication['canonical_commit_sha']}/{path}"
            ),
            artifact_path=path,
            artifact_title="Failed-payment emails",
            summary=(
                f"Approve {count} failed-payment email{'s' if count != 1 else ''} from "
                f"{scope['gmail']['email']}, each checked against Stripe right before it is sent."
                + test
            ),
        )
        if not required:
            raise ApplicationError(
                "Recovery emails must require explicit approval.", non_retryable=True
            )

    @activity.defn(name="payment_recovery_record_approval")
    async def record_approval(self, run_id: str) -> None:
        run = await self._active(run_id)
        await self.db.record_human_review(
            run_id=run.id, decision="approved", summary="You approved the failed-payment emails."
        )

    @activity.defn(name="payment_recovery_apply")
    async def apply(self, run_id: str) -> None:
        """Send each approved email once, only while its invoice is still open in Stripe."""
        if await self._result(run_id, "applied"):
            return
        draft = await self._result(run_id, "draft")
        scope = await self._result(run_id, "scope")
        evidence = await self._result(run_id, "evidence")
        run = await self._active(run_id)
        if run.review_decision != "approved":
            raise ApplicationError("The emails were not approved.", non_retryable=True)
        amounts = {c["invoice_id"]: c["amount"] for c in evidence["cases"]}
        base = [
            {k: e[k] for k in ("invoice_id", "customer_id", "to", "name")}
            | {"amount": amounts.get(e["invoice_id"], "")}
            for e in draft["emails"]
        ]
        if not scope["stripe"]["livemode"]:
            results = [
                {**b, "status": "skipped", "reason": "Stripe test mode; not sent"} for b in base
            ]
            await self._save(run_id, "applied", {"results": results})
            return
        await self._progress(run_id, "apply", 4, "Sending the approved emails")
        stripe = await self._stripe_connection(run_id, run.project_id, scope)
        gmail = await self._gmail_connection(run_id, run.project_id)
        if gmail.external_account_id != scope["gmail"]["external_account_id"]:
            await self._refuse(
                run_id,
                "A different Gmail account is connected than the one you approved; "
                "nothing was sent.",
            )
        results, sent_before = [], False
        for email, row in zip(draft["emails"], base, strict=True):
            if sent_before:
                await self.sleep(POLICY["send_interval_seconds"])
            try:
                result = await self._send(run, scope, email, row, stripe)
            except (*STRIPE_STOPS, MailboxStopped) as exc:
                # Gmail or Stripe stopped accepting Tin's access. A timeout or rate limit is
                # retried by the activity instead; receipts keep each invoice to one send.
                sent = sum(1 for r in results if r["status"] == "sent")
                await self._refuse(
                    run_id,
                    f"{str(exc).rstrip('.')}. {sent} email(s) were sent before this stopped. Fix "
                    "it, then run it again; invoices already emailed are skipped.",
                )
            sent_before = result["status"] == "sent" and not result.get("replayed")
            results.append(result)
            if activity.in_activity():
                activity.heartbeat({"invoice": email["invoice_id"]})
        await self._save(run_id, "applied", {"results": results})

    async def _send(self, run, scope, email, row, stripe) -> dict:
        key = self.invoice_key(run.project_id, email["invoice_id"])
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            saved = (existing.result or {}) if existing else {}
            if existing and saved.get("run_id") != str(run.id):
                # Another run sent, or may have sent, this one; never send it again from here.
                return {**row, "status": "skipped", "reason": "already emailed by another run"}
            if existing and existing.status == "completed":
                return {**row, **saved, "replayed": True}
            # A started receipt of this run may have reached Gmail: the send below looks the
            # message up by its Message-ID before posting, so it is retried, never re-checked.
            first = existing is None
            if first:
                still_open = await self.integrations.stripe.call(
                    "invoices.list",
                    {"customer": email["customer_id"], "status": "open", "limit": 100},
                    connection=stripe,
                    run_id=run.id,
                    execution_key=f"{key}:recheck:{run.id}",
                    max_response_bytes=None,
                )
                if email["invoice_id"] not in {i["id"] for i in still_open["records"]}:
                    return {**row, "status": "skipped", "reason": "paid or closed since approval"}
                await self.db.start_effect(conn, execution_key=key, operation=KEY)
                await self.db.save_effect_progress(
                    conn,
                    execution_key=key,
                    result={"run_id": str(run.id), "attempted_at": datetime.now(UTC).isoformat()},
                )
            try:
                sent = await self.integrations.workspace_send_message(
                    project_id=run.project_id,
                    run_id=run.id,
                    connection_id=UUID(scope["gmail"]["connection_id"]),
                    external_account_id=scope["gmail"]["external_account_id"],
                    execution_key=f"{key}:gmail:{run.id}",
                    recipient_email=email["to"],
                    recipient_name=email["name"] or "",
                    subject=email["subject"],
                    body=email["body"],
                )
                result = {
                    "run_id": str(run.id),
                    "status": "sent",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "thread_id": sent.get("thread_id"),
                }
            except IntegrationDeliveryUnknownError:
                result = {
                    "run_id": str(run.id),
                    "status": "unknown",
                    "reason": "Gmail did not confirm delivery; check your Sent folder",
                }
            except IntegrationAuthorizationError:
                # Refused before Gmail was asked; only a first attempt can be sure of that.
                if first:
                    await self.db.discard_started_effect(conn, execution_key=key)
                raise
            except IntegrationDeliveryRefusedError as exc:
                # Gmail answered and sent nothing, so a later run may try this invoice again.
                await self.db.discard_started_effect(conn, execution_key=key)
                if exc.status in {401, 403}:
                    raise MailboxStopped("Gmail no longer accepts Tin's access") from None
                if exc.status == 429:
                    raise MailboxStopped("Gmail's sending limit was reached") from None
                return {**row, "status": "refused", "reason": str(exc)}
            # Any other failure (a timeout, a lookup that did not answer) keeps the receipt
            # started and is retried; the retry finds a delivered message before posting.
            await self.db.complete_effect(conn, execution_key=key, result=result)
            return {**row, **result}

    @activity.defn(name="payment_recovery_publish")
    async def publish(self, run_id: str) -> None:
        scope = await self._result(run_id, "scope")
        evidence = await self._result(run_id, "evidence")
        applied = await self._result(run_id, "applied") or {"results": []}
        run = await self._active(run_id)
        project = await self.db.get_project(run.project_id)
        await self._progress(run_id, "publish", 5, "Saving the result to your project files")
        names = paths(run_id, RESULT_DOCS)
        results = [{k: v for k, v in r.items() if k != "replayed"} for r in applied["results"]]
        skipped = [*evidence["earlier"], *evidence["skipped"]]
        if results:
            document = payment_recovery.render_result(
                sender=scope["gmail"]["email"],
                product=scope["product"],
                results=results,
                skipped=skipped,
            )
            summary = payment_recovery.summary_line(results)
        else:
            document = payment_recovery.render_empty(
                product=scope["product"],
                lookback_days=scope["inputs"]["lookback_days"],
                counts=evidence["counts"],
                skipped=skipped,
            )
            summary = "No failed payments to recover right now."
        publication = await self._publish(
            run_id, "publish", {"RESULT.md": document}, names, project
        )
        key = self.key(run_id, "projection")
        async with self.db.effect_lock(key, KEY) as (conn, existing):
            if existing and existing.status == "completed":
                return
            await self.db.start_effect(conn, execution_key=key, operation=KEY)
            await self.db.complete_payment_recovery_projection(
                conn,
                execution_key=key,
                run_id=run.id,
                canonical_commit_sha=publication["canonical_commit_sha"],
                artifact_path=names["RESULT.md"],
                artifact_ref=(
                    f"code.storage://{project.state_repo_id}@"
                    f"{publication['canonical_commit_sha']}/{names['RESULT.md']}"
                ),
                summary=summary,
                approved=bool(results),
            )

    @activity.defn(name="payment_recovery_failure")
    async def failure(self, run_id: str) -> None:
        run = await self.db.get_run(UUID(run_id))
        if run is None or run.executor != KEY:
            return
        failure = await self._result(run_id, "failure") or {}
        await self.db.project_failure(
            run_id=run.id,
            error_message=failure.get("detail")
            or "The payment recovery stopped before finishing. Check your Sent folder before "
            "trying again; Tin never emails about the same invoice twice.",
        )
