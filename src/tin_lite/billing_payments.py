"""Dedicated Tin Lite test payments; shared Stripe account, separate product and webhook."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import stripe

from tin_lite.billing_contracts import NANOS_PER_CENT, BillingError, usd
from tin_lite.product_urls import dashboard_url

PRODUCT_ID = "tin_lite_prepaid_test_v1"
EVENTS = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
    "invoice.paid",
    "refund.created",
    "refund.updated",
    "refund.failed",
    "charge.dispute.created",
    "charge.dispute.closed",
)


class StripePayments:
    def __init__(self, *, billing, settings, transport=None):
        self.billing, self.db, self.settings = billing, billing.db, settings
        self.transport = transport

    def key(self):
        value = getattr(self.settings, "stripe_secret_key", None)
        if not getattr(self.settings, "billing_test_enabled", False) or not value:
            raise BillingError(
                "payments_unavailable", "Stripe test payments are not configured.", 503
            )
        key = value.get_secret_value()
        if not key.startswith(("sk_test_", "rk_test_")):
            raise BillingError("wrong_payment_mode", "Use a Stripe test-mode key.", 503)
        return key

    async def request(self, method, path, *, data=None, idempotency_key=None, allow_missing=False):
        headers = {"Authorization": f"Bearer {self.key()}", "Stripe-Version": "2024-06-20"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=25) as client:
                response = await client.request(
                    method, f"https://api.stripe.com/v1/{path}", headers=headers, data=data
                )
            if allow_missing and response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise BillingError(
                    "payment_provider_unavailable",
                    "Stripe could not complete this request. Retry the same request.",
                    502,
                )
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BillingError(
                "payment_status_unknown", "Payment status unavailable. Retry the same request.", 502
            ) from exc

    async def ensure_product(self):
        product = await self.request("GET", f"products/{PRODUCT_ID}", allow_missing=True)
        if product is None:
            product = await self.request(
                "POST",
                "products",
                idempotency_key="tin-lite:test-product:v1",
                data={
                    "id": PRODUCT_ID,
                    "name": "Tin Lite usage — test",
                    "metadata[tin_product]": "tin-lite",
                },
            )
        if (
            product.get("livemode") is not False
            or product.get("metadata", {}).get("tin_product") != "tin-lite"
        ):
            raise BillingError(
                "product_mismatch", "The Tin Lite test product could not be verified."
            )
        return product["id"]

    async def checkout(self, *, workspace_id, actor, amount_cents, request_id):
        self.key()
        if type(amount_cents) is not int or not 1000 <= amount_cents <= 100000:
            raise BillingError(
                "invalid_amount", "Choose a test top-up between $10 and $1,000.", 422
            )
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.billing.require_admin(conn, workspace_id, actor, lock=True)
            prior = await conn.fetchrow(
                "SELECT * FROM billing_payments WHERE workspace_id=$1 AND request_id=$2",
                workspace_id,
                request_id,
            )
            if prior and (
                prior["actor_clerk_user_id"] != actor or prior["amount_cents"] != amount_cents
            ):
                raise BillingError(
                    "request_conflict", "This payment request has different recorded terms."
                )
            payment = prior or await conn.fetchrow(
                """INSERT INTO billing_payments(id, workspace_id, actor_clerk_user_id,
                   request_id, amount_cents)
                   VALUES($1,$2,$3,$4,$5) RETURNING *""",
                uuid4(),
                workspace_id,
                actor,
                request_id,
                amount_cents,
            )
        if payment["stripe_session_id"]:
            return self.payment_view(payment)
        if payment["created_at"] < datetime.now(UTC) - timedelta(hours=23):
            raise BillingError(
                "payment_needs_reconciliation",
                "This payment needs reconciliation before another checkout.",
            )
        product = await self.ensure_product()
        base = dashboard_url(self.settings)
        response = await self.request(
            "POST",
            "checkout/sessions",
            idempotency_key=f"tin-lite:checkout:{payment['id']}",
            data={
                "mode": "payment",
                "payment_method_types[0]": "card",
                "customer_creation": "always",
                "line_items[0][price_data][currency]": "usd",
                "line_items[0][price_data][product]": product,
                "line_items[0][price_data][unit_amount]": str(amount_cents),
                "line_items[0][quantity]": "1",
                "invoice_creation[enabled]": "true",
                "invoice_creation[invoice_data][metadata][tin_product]": "tin-lite",
                "invoice_creation[invoice_data][metadata][tin_payment_id]": str(payment["id"]),
                "metadata[tin_product]": "tin-lite",
                "metadata[tin_payment_id]": str(payment["id"]),
                "payment_intent_data[metadata][tin_product]": "tin-lite",
                "payment_intent_data[metadata][tin_payment_id]": str(payment["id"]),
                "success_url": f"{base}/billing?billing_payment={payment['id']}",
                "cancel_url": f"{base}/billing?billing_payment={payment['id']}",
            },
        )
        if response.get("livemode") is not False or not str(response.get("url", "")).startswith(
            "https://checkout.stripe.com/"
        ):
            raise BillingError(
                "checkout_unavailable", "A valid test checkout was not returned.", 502
            )
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self.billing.require_admin(conn, workspace_id, actor, lock=True)
            row = await conn.fetchrow(
                """UPDATE billing_payments SET stripe_session_id=$2, checkout_url=$3
                   WHERE id=$1 AND (stripe_session_id IS NULL OR stripe_session_id=$2)
                   RETURNING *""",
                payment["id"],
                response["id"],
                response["url"],
            )
            if row is None:
                raise BillingError(
                    "checkout_conflict", "The payment already belongs to a different checkout."
                )
        return self.payment_view(row)

    def payment_view(self, payment, *, available_cents=0):
        return {
            "id": str(payment["id"]),
            "mode": "test",
            "status": payment["status"],
            "amount_usd": usd(payment["amount_cents"] * NANOS_PER_CENT),
            "checkout_url": payment["checkout_url"] if payment["status"] == "pending" else None,
            "invoice_url": payment["invoice_url"],
            "refundable_usd": usd(
                max(
                    0,
                    min(
                        available_cents,
                        payment["amount_cents"]
                        - payment["consumed_cents"]
                        - payment["refunded_cents"]
                        - payment["refund_reserved_cents"],
                    ),
                )
                * NANOS_PER_CENT
            )
            if payment["status"] == "paid"
            else "0.00",
            "refund_pending_usd": usd(payment["refund_reserved_cents"] * NANOS_PER_CENT),
        }

    async def list_payments(self, workspace_id, actor):
        async with (
            self.db.pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            account = await self.billing.require_admin(conn, workspace_id, actor)
            rows = await conn.fetch(
                """SELECT * FROM billing_payments WHERE workspace_id=$1
                   ORDER BY created_at DESC LIMIT 100""",
                workspace_id,
            )
            available = (
                (account["balance_nanos"] - account["reserved_nanos"]) // NANOS_PER_CENT
                if account["status"] == "active"
                else 0
            )
            return [self.payment_view(row, available_cents=available) for row in rows]

    async def checkout_return(self, payment_id, actor):
        """Resolve an opaque Checkout return to its authorized workspace, without effects."""
        async with self.db.pool.acquire() as conn, conn.transaction(readonly=True):
            payment = await conn.fetchrow(
                "SELECT workspace_id FROM billing_payments WHERE id=$1", payment_id
            )
            if payment is None:
                raise LookupError("billing payment not found")
            await self.billing.require_admin(conn, payment["workspace_id"], actor)
            # The shell chooses only from its separately membership-gated project list.
            # A redirect parameter never credits funds or establishes payment success.
            return {"workspace_id": str(payment["workspace_id"])}

    async def reconcile(self):
        """Recover lost provider acknowledgments/webhooks using recorded IDs and intents."""
        if not getattr(self.settings, "billing_test_enabled", False) or not getattr(
            self.settings, "stripe_secret_key", None
        ):
            return
        # Session-less requests past Stripe's retry window await operator reconciliation;
        # they must not fill the window ahead of rows this pass can still recover.
        payments = await self.db.pool.fetch(
            """SELECT * FROM billing_payments WHERE status='pending'
               AND created_at<now()-interval '30 seconds'
               AND (stripe_session_id IS NOT NULL OR created_at>now()-interval '23 hours')
               ORDER BY created_at LIMIT 20"""
        )
        failure = None
        for payment in payments:
            try:
                if payment["stripe_session_id"]:
                    obj = await self.request(
                        "GET", f"checkout/sessions/{payment['stripe_session_id']}"
                    )
                    if (
                        obj.get("metadata", {}).get("tin_payment_id") != str(payment["id"])
                        or obj.get("metadata", {}).get("tin_product") != "tin-lite"
                    ):
                        raise BillingError("payment_mismatch", "Checkout identity did not match.")
                    status = obj.get("status")
                    await self.handle_event(
                        {
                            "id": (
                                f"reconcile:checkout:{obj['id']}:{status}:"
                                f"{obj.get('payment_status')}"
                            ),
                            "type": "checkout.session.expired"
                            if status == "expired"
                            else "checkout.session.completed",
                            "livemode": obj.get("livemode"),
                            "data": {"object": obj},
                        }
                    )
                elif payment["created_at"] > datetime.now(UTC) - timedelta(hours=23):
                    # The same durable request stays within Stripe's idempotency window.
                    await self.checkout(
                        workspace_id=payment["workspace_id"],
                        actor=payment["actor_clerk_user_id"],
                        amount_cents=payment["amount_cents"],
                        request_id=payment["request_id"],
                    )
            except Exception as exc:
                # One unreconciled row must not starve later rows or refunds.
                failure = failure or exc
        refunds = await self.db.pool.fetch(
            """SELECT * FROM billing_refunds WHERE status='pending'
               AND created_at<now()-interval '30 seconds'
               AND (stripe_refund_id IS NOT NULL OR created_at>now()-interval '23 hours')
               ORDER BY created_at LIMIT 20"""
        )
        for refund in refunds:
            try:
                if refund["stripe_refund_id"]:
                    obj = await self.request("GET", f"refunds/{refund['stripe_refund_id']}")
                    if obj.get("id") != refund["stripe_refund_id"]:
                        raise BillingError("refund_mismatch", "Refund identity did not match.")
                    async with self.db.pool.acquire() as conn, conn.transaction():
                        await self._refund_event(conn, obj)
                elif refund["created_at"] > datetime.now(UTC) - timedelta(hours=23):
                    await self.request_refund(
                        refund["payment_id"],
                        refund["actor_clerk_user_id"],
                        refund["request_id"],
                        refund["amount_cents"],
                    )
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def webhook(self, body, signature):
        secret = getattr(self.settings, "stripe_webhook_secret", None)
        if not secret or not getattr(self.settings, "billing_test_enabled", False):
            raise BillingError("webhook_unavailable", "Stripe webhook is not configured.", 503)
        try:
            event = stripe.Webhook.construct_event(
                body, signature, secret.get_secret_value(), tolerance=300
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise BillingError("invalid_signature", "Invalid Stripe signature.", 400) from exc
        return await self.handle_event(event.to_dict())

    async def handle_event(self, event):
        """Called after signature verification (or directly by transport fixtures)."""
        if event.get("livemode") is not False or event.get("type") not in EVENTS:
            return {"received": True, "ignored": True}
        event_type, obj = event["type"], event.get("data", {}).get("object", {})
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Serialize duplicates without persisting the raw event (which includes payer PII).
            inserted = await conn.fetchval(
                """INSERT INTO billing_payment_events(stripe_event_id,event_type) VALUES($1,$2)
                   ON CONFLICT(stripe_event_id) DO NOTHING RETURNING stripe_event_id""",
                event["id"],
                event_type,
            )
            if not inserted:
                return {"received": True}
            if event_type.startswith("checkout.") or event_type == "invoice.paid":
                meta = obj.get("metadata") or {}
                if meta.get("tin_product") != "tin-lite":
                    return {"received": True, "ignored": True}
                try:
                    payment_id = UUID(meta.get("tin_payment_id", ""))
                except (ValueError, TypeError):
                    return {"received": True, "ignored": True}
                payment = await conn.fetchrow(
                    "SELECT * FROM billing_payments WHERE id=$1", payment_id
                )
                if not payment:
                    raise BillingError(
                        "payment_unknown", "Payment is not available for reconciliation."
                    )
                account = await conn.fetchrow(
                    "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
                    payment["workspace_id"],
                )
                payment = await conn.fetchrow(
                    "SELECT * FROM billing_payments WHERE id=$1 FOR UPDATE", payment_id
                )
                if obj.get("livemode") is not False or obj.get("currency") != "usd":
                    raise BillingError(
                        "payment_mismatch", "Payment mode or currency did not match."
                    )
                if event_type == "invoice.paid":
                    if obj.get("amount_paid") != payment["amount_cents"]:
                        raise BillingError("invoice_mismatch", "Invoice amount did not match.")
                    url = obj.get("hosted_invoice_url")
                    if not isinstance(url, str) or not url.startswith(
                        "https://invoice.stripe.com/"
                    ):
                        raise BillingError("invoice_unavailable", "Invoice URL is not available.")
                    await conn.execute(
                        "UPDATE billing_payments SET invoice_id=$2, invoice_url=$3 WHERE id=$1",
                        payment_id,
                        obj["id"],
                        url,
                    )
                else:
                    if (
                        payment["stripe_session_id"] not in {None, obj["id"]}
                        or obj.get("amount_total") != payment["amount_cents"]
                        or obj.get("mode") != "payment"
                    ):
                        raise BillingError(
                            "payment_mismatch", "Payment did not match the recorded checkout."
                        )
                    if event_type == "checkout.session.expired":
                        await conn.execute(
                            """UPDATE billing_payments SET status='expired'
                               WHERE id=$1 AND status='pending'""",
                            payment_id,
                        )
                    elif obj.get("payment_status") == "paid":
                        intent = obj.get("payment_intent")
                        if not isinstance(intent, str):
                            raise BillingError(
                                "payment_pending", "Payment confirmation is incomplete."
                            )
                        await self.billing.post_ledger(
                            conn,
                            account=account,
                            event_key=f"payment:{payment_id}:credit",
                            kind="topup",
                            amount=payment["amount_cents"] * NANOS_PER_CENT,
                            reference=str(payment_id),
                        )
                        await conn.execute(
                            """UPDATE billing_payments SET status='paid', stripe_session_id=$2,
                               stripe_payment_intent_id=$3 WHERE id=$1""",
                            payment_id,
                            obj["id"],
                            intent,
                        )
            elif event_type.startswith("refund."):
                await self._refund_event(conn, obj)
            elif event_type.startswith("charge.dispute."):
                await self._dispute_event(conn, obj)
        return {"received": True}

    async def request_refund(self, payment_id, actor, request_id, amount_cents):
        self.key()
        if type(amount_cents) is not int or amount_cents <= 0:
            raise BillingError("invalid_amount", "Enter a positive refund amount.", 422)
        async with self.db.pool.acquire() as conn, conn.transaction():
            payment = await conn.fetchrow("SELECT * FROM billing_payments WHERE id=$1", payment_id)
            if not payment:
                raise LookupError("payment not found")
            account = await self.billing.require_admin(
                conn, payment["workspace_id"], actor, lock=True
            )
            payment = await conn.fetchrow(
                "SELECT * FROM billing_payments WHERE id=$1 FOR UPDATE", payment_id
            )
            refund = await conn.fetchrow(
                "SELECT * FROM billing_refunds WHERE payment_id=$1 AND request_id=$2",
                payment_id,
                request_id,
            )
            if refund and (
                refund["actor_clerk_user_id"] != actor or refund["amount_cents"] != amount_cents
            ):
                raise BillingError("request_conflict", "This refund request has different terms.")
            if not refund:
                eligible = min(
                    payment["amount_cents"]
                    - payment["consumed_cents"]
                    - payment["refunded_cents"]
                    - payment["refund_reserved_cents"],
                    (account["balance_nanos"] - account["reserved_nanos"]) // NANOS_PER_CENT,
                )
                if (
                    payment["status"] != "paid"
                    or account["status"] != "active"
                    or amount_cents > eligible
                ):
                    raise BillingError(
                        "refund_unavailable", "That amount is not available to refund."
                    )
                refund = await conn.fetchrow(
                    """INSERT INTO billing_refunds(id,payment_id,request_id,
                       actor_clerk_user_id,amount_cents)
                       VALUES($1,$2,$3,$4,$5) RETURNING *""",
                    uuid4(),
                    payment_id,
                    request_id,
                    actor,
                    amount_cents,
                )
                await conn.execute(
                    """UPDATE billing_payments SET refund_reserved_cents=refund_reserved_cents+$2
                       WHERE id=$1""",
                    payment_id,
                    amount_cents,
                )
                await conn.execute(
                    """UPDATE billing_accounts SET reserved_nanos=reserved_nanos+$2
                       WHERE workspace_id=$1""",
                    account["workspace_id"],
                    amount_cents * NANOS_PER_CENT,
                )
        if refund["status"] != "pending" or refund["stripe_refund_id"]:
            return {"id": str(refund["id"]), "status": refund["status"]}
        if refund["created_at"] < datetime.now(UTC) - timedelta(hours=23):
            raise BillingError(
                "refund_needs_reconciliation",
                "This refund needs reconciliation before another provider request.",
            )
        response = await self.request(
            "POST",
            "refunds",
            idempotency_key=f"tin-lite:refund:{refund['id']}",
            data={
                "payment_intent": payment["stripe_payment_intent_id"],
                "amount": str(amount_cents),
                "metadata[tin_product]": "tin-lite",
                "metadata[tin_refund_id]": str(refund["id"]),
            },
        )
        async with self.db.pool.acquire() as conn, conn.transaction():
            await self._refund_event(conn, response)
        return {"id": str(refund["id"]), "status": response.get("status", "pending")}

    async def _refund_event(self, conn, obj):
        meta = obj.get("metadata") or {}
        if meta.get("tin_product") != "tin-lite" or not meta.get("tin_refund_id"):
            # A Stripe-dashboard refund has no Tin refund metadata. Reconcile it
            # by our recorded payment, never by another product's customer identity.
            payment = await self._payment_for_intent(conn, obj.get("payment_intent"))
            if not payment:
                return
            await self._external_refund(conn, payment, obj)
            return
        try:
            refund_id = UUID(meta.get("tin_refund_id", ""))
        except (ValueError, TypeError):
            return
        refund = await conn.fetchrow("SELECT * FROM billing_refunds WHERE id=$1", refund_id)
        if not refund:
            raise BillingError("refund_unknown", "Refund is not available for reconciliation.")
        payment = await conn.fetchrow(
            "SELECT * FROM billing_payments WHERE id=$1", refund["payment_id"]
        )
        account = await conn.fetchrow(
            "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
            payment["workspace_id"],
        )
        refund = await conn.fetchrow(
            "SELECT * FROM billing_refunds WHERE id=$1 FOR UPDATE", refund_id
        )
        if (
            obj.get("amount") != refund["amount_cents"]
            or obj.get("currency") != "usd"
            or obj.get("payment_intent") != payment["stripe_payment_intent_id"]
            or refund["stripe_refund_id"] not in {None, obj["id"]}
        ):
            raise BillingError("refund_mismatch", "Refund did not match the recorded request.")
        if refund["status"] == "succeeded" and obj.get("status") == "failed":
            # A card refund can fail after succeeding; Stripe returns the funds to us.
            await self.billing.post_ledger(
                conn,
                account=account,
                event_key=f"refund:{refund_id}:reversal",
                kind="adjustment",
                amount=refund["amount_cents"] * NANOS_PER_CENT,
                reference=str(refund_id),
            )
            await conn.execute(
                "UPDATE billing_payments SET refunded_cents=refunded_cents-$2 WHERE id=$1",
                payment["id"],
                refund["amount_cents"],
            )
            await conn.execute("UPDATE billing_refunds SET status='failed' WHERE id=$1", refund_id)
            return
        if refund["status"] != "pending":
            return
        await conn.execute(
            "UPDATE billing_refunds SET stripe_refund_id=$2 WHERE id=$1", refund_id, obj["id"]
        )
        result = obj.get("status")
        if result not in {"succeeded", "failed", "canceled"}:
            return
        amount = refund["amount_cents"] * NANOS_PER_CENT
        if result == "succeeded":
            await self.billing.post_ledger(
                conn,
                account=account,
                event_key=f"refund:{refund_id}",
                kind="refund",
                amount=-amount,
                reference=str(refund_id),
            )
        await conn.execute(
            "UPDATE billing_accounts SET reserved_nanos=reserved_nanos-$2 WHERE workspace_id=$1",
            account["workspace_id"],
            amount,
        )
        await conn.execute(
            """UPDATE billing_payments SET refund_reserved_cents=refund_reserved_cents-$2,
               refunded_cents=refunded_cents+$3 WHERE id=$1""",
            payment["id"],
            refund["amount_cents"],
            refund["amount_cents"] if result == "succeeded" else 0,
        )
        await conn.execute(
            "UPDATE billing_refunds SET status=$2 WHERE id=$1",
            refund_id,
            "succeeded" if result == "succeeded" else "failed",
        )

    async def _payment_for_intent(self, conn, intent_id):
        if not isinstance(intent_id, str) or not intent_id.startswith("pi_"):
            return None
        payment = await conn.fetchrow(
            "SELECT * FROM billing_payments WHERE stripe_payment_intent_id=$1",
            intent_id,
        )
        if payment:
            return payment
        # Delivery can precede Checkout's paid event. A verified read identifies
        # our payment; returning an error asks Stripe to retry after funding lands.
        from urllib.parse import quote

        intent = await self.request("GET", f"payment_intents/{quote(intent_id, safe='')}")
        meta = intent.get("metadata") or {}
        if intent.get("livemode") is not False or meta.get("tin_product") != "tin-lite":
            return None
        raise BillingError("payment_pending", "Waiting for the Tin payment confirmation.", 503)

    async def _external_refund(self, conn, payment, obj):
        if (
            obj.get("currency") != "usd"
            or type(obj.get("amount")) is not int
            or not 0 < obj["amount"] <= payment["amount_cents"]
        ):
            raise BillingError("refund_mismatch", "Refund amount did not match the payment.")
        account = await conn.fetchrow(
            "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
            payment["workspace_id"],
        )
        refund_id = uuid5(NAMESPACE_URL, f"tin-lite:stripe-refund:{obj['id']}")
        prior = await conn.fetchrow("SELECT id FROM billing_refunds WHERE id=$1", refund_id)
        if not prior:
            await conn.execute(
                """INSERT INTO billing_refunds(id,payment_id,request_id,actor_clerk_user_id,
                    amount_cents) VALUES($1,$2,$1,'stripe_dashboard',$3)""",
                refund_id,
                payment["id"],
                obj["amount"],
            )
            await conn.execute(
                """UPDATE billing_payments SET refund_reserved_cents=refund_reserved_cents+$2
                   WHERE id=$1""",
                payment["id"],
                obj["amount"],
            )
            await conn.execute(
                """UPDATE billing_accounts SET reserved_nanos=reserved_nanos+$2, status='suspended'
                   WHERE workspace_id=$1""",
                account["workspace_id"],
                obj["amount"] * NANOS_PER_CENT,
            )
        await self._refund_event(
            conn,
            {
                **obj,
                "metadata": {
                    "tin_product": "tin-lite",
                    "tin_refund_id": str(refund_id),
                },
            },
        )

    async def _dispute_event(self, conn, obj):
        payment = await self._payment_for_intent(conn, obj.get("payment_intent"))
        if not payment:
            return  # Never touch another product's money on the shared Stripe account.
        account = await conn.fetchrow(
            "SELECT * FROM billing_accounts WHERE workspace_id=$1 FOR UPDATE",
            payment["workspace_id"],
        )
        if (
            obj.get("currency") != "usd"
            or type(obj.get("amount")) is not int
            or not 0 < obj["amount"] <= payment["amount_cents"]
        ):
            raise BillingError("dispute_mismatch", "Dispute amount did not match the payment.")
        prior = await conn.fetchrow(
            "SELECT * FROM billing_disputes WHERE stripe_dispute_id=$1", obj["id"]
        )
        if prior and prior["status"] in {"won", "lost"}:
            return
        status = obj.get("status") if obj.get("status") in {"won", "lost"} else "open"
        if obj.get("status") == "warning_closed":
            # An inquiry that closes without a chargeback never withdrew the funds.
            status = "won"
        if not prior:
            await self.billing.post_ledger(
                conn,
                account=account,
                event_key=f"dispute:{obj['id']}:debit",
                kind="dispute",
                amount=-obj["amount"] * NANOS_PER_CENT,
                reference=obj["id"],
            )
        if status == "won":
            await self.billing.post_ledger(
                conn,
                account=account,
                event_key=f"dispute:{obj['id']}:restore",
                kind="adjustment",
                amount=obj["amount"] * NANOS_PER_CENT,
                reference=obj["id"],
            )
        await conn.execute(
            """INSERT INTO billing_disputes(stripe_dispute_id,payment_id,amount_cents,status)
               VALUES($1,$2,$3,$4) ON CONFLICT(stripe_dispute_id) DO UPDATE SET status=$4
               WHERE billing_disputes.stripe_dispute_id=$1""",
            obj["id"],
            payment["id"],
            obj["amount"],
            status,
        )
        # Re-enabling after a dispute is explicit; no stale event can resume spending.
        await conn.execute(
            "UPDATE billing_accounts SET status='suspended' WHERE workspace_id=$1",
            account["workspace_id"],
        )
