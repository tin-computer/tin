"""Run-bound service calls through existing project integration capabilities."""

import asyncio
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime

import httpx

from tin_lite import managed_services, posthog_connection, stripe_connection
from tin_lite.billing_contracts import BillingError, digest
from tin_lite.connection_records import ServiceArgumentError
from tin_lite.integrations import (
    POSTHOG_PROVIDER,
    STRIPE_PROVIDER,
    IntegrationError,
    IntegrationProviderError,
    IntegrationRequirement,
    ServiceCallRefused,
    ServiceResponseTooLarge,
    check_google_arguments,
    parse_integration_requirements,
)
from tin_lite.project_connections import (
    CUSTOM_KEY,
    READ_METHODS,
    InvalidAPIResponse,
    request_api,
    request_contract,
)
from tin_lite.provider_errors import explain

logger = logging.getLogger(__name__)

OPERATION = "code_service_call_v1"
# The state of a run's optional connections, pinned at its first execution.
CONNECTIONS = "code_service_connections_v1"
# Adding an adapter operation is an explicit reviewed mapping, never getattr on author input.
OPERATIONS = {
    ("analytics.gsc", "sites.list"): ("sites.list", frozenset()),
    ("analytics.gsc", "search_analytics.read"): (
        "search_analytics.read",
        frozenset(
            {"start_date", "end_date", "dimensions", "row_limit", "start_row", "dimension_filters"}
        ),
    ),
    ("infra.github", "repositories.list"): ("repositories.list", frozenset()),
    ("workspace.google", "gmail.messages.search"): (
        "gmail.messages.read",
        frozenset({"query", "max_results"}),
    ),
    ("workspace.google", "gmail.thread.read"): ("gmail.messages.read", frozenset({"thread_id"})),
    ("workspace.google", "calendar.events.list"): (
        "calendar.events.read",
        frozenset({"time_min", "time_max", "query", "max_results"}),
    ),
    # Stripe's reviewed read table: operation name -> (capability, closed argument names).
    **{
        (STRIPE_PROVIDER, name): (op.capability, op.arguments)
        for name, op in stripe_connection.OPERATIONS.items()
    },
    # PostHog's reviewed reads; the selected project is Tin's, never an argument.
    **{
        (POSTHOG_PROVIDER, name): (op.capability, op.arguments)
        for name, op in posthog_connection.OPERATIONS.items()
    },
    # Services Tin holds the key for; see managed_services.py.
    **{key: (op.capability, op.arguments) for key, op in managed_services.OPERATIONS.items()},
}


def _check_google_arguments(operation, args):
    try:
        check_google_arguments(operation, args)
    except IntegrationError as exc:
        raise ServiceArgumentError(str(exc)) from None


# Providers whose argument values are checked before a receipt exists, so a malformed call is
# a contract error the author can fix rather than an uncertain provider attempt.
ARGUMENT_CHECKS = {
    STRIPE_PROVIDER: stripe_connection.check_arguments,
    POSTHOG_PROVIDER: posthog_connection.check_arguments,
    "analytics.gsc": _check_google_arguments,
    "workspace.google": _check_google_arguments,
    **{provider: managed_services.check_arguments for provider in managed_services.DEFINITIONS},
}
# Tin's own wait for any one provider call; managed Google reads stop a few seconds sooner.
CALL_SECONDS = 60
# Tin's sentence plus the provider's own message (itself cut to 1,500 characters).
MESSAGE_LIMIT = 2000


class CodeServiceError(ValueError):
    """Tin's message for a failed service call; never supplier exceptions, URLs or credentials.

    When the provider answered and refused, the message ends with what it said ("PostHog said:
    validation_error/invalid_input: ..."), and `provider_error` holds the same as a dict:
    status, error type and code, and its message, cut and redacted (provider_errors.py).
    `code` names Tin's refusal kind, such as `query_error` or `rate_limited`.

    Authored code receives the message as a ValueError from the call, unless `fatal`: the run
    itself lost its authority, so it stops without handing anything back.
    """

    def __init__(self, message, *, fatal=False, code=None, provider_error=None):
        super().__init__(message)
        self.fatal = fatal
        self.code = code
        self.provider_error = provider_error

    def diagnostic(self):
        """What a procedure's `call_service` tool error carries, as JSON."""
        value = {"code": self.code or "service_error", "message": str(self)}
        if self.provider_error:
            value["provider_error"] = self.provider_error
        return value


SPENDING_STOPPED = (
    "Tin credits or project spending limits do not allow this paid read; no request was made."
)


def _too_large(service):
    # The bound is the author's own declared value; naming it lets them size the next request.
    return CodeServiceError(
        f"The service response exceeded this binding's max_response_bytes "
        f"({service.max_response_bytes}); request less data, for example a smaller "
        "row_limit or the next start_row page.",
        code="response_too_large",
    )


def optional_services(workflow, spec):
    """The bindings whose integration requirement is optional in the pinned definition."""
    optional = {
        r.provider_key
        for r in parse_integration_requirements(workflow.definition.get("integration_requirements"))
        if not r.required
    }
    return [s for s in spec.services if s.provider_key in optional]


def _connections_key(run):
    return f"{run.id}:code-service-connections"


class CodeServices:
    def __init__(
        self,
        *,
        database,
        integrations,
        authorize,
        client=None,
        resolver=None,
        settings=None,
        managed=None,
    ):
        self.db, self.integrations, self.authorize = database, integrations, authorize
        self.client, self.resolver = client, resolver
        # Tin-held provider keys come from the switchboard's settings, never the package.
        self.managed = managed or managed_services.ManagedServices(settings)

    async def connections(self, *, conn, run, workflow, spec):
        """Each optional binding's state for this run: connected, not_connected or
        needs_attention. The first execution pins it, so a retry sees the same answer and a
        connection made mid-run waits for the next run. The code reads it as ctx["connections"].
        """
        optional = optional_services(workflow, spec)
        if not optional:
            return {}
        key = _connections_key(run)
        saved = await self.db.get_effect(key, conn=conn)
        if saved and saved.status == "completed":
            return dict((saved.result or {}).get("connections") or {})
        states = {}
        for service in optional:
            requirement = IntegrationRequirement(service.provider_key, service.capabilities)
            try:
                await self.integrations.ensure_requirements(
                    project_id=run.project_id, requirements=(requirement,)
                )
                states[service.name] = "connected"
            except IntegrationError:
                connection = await self.db.get_integration_connection(
                    project_id=run.project_id, provider_key=service.provider_key
                )
                states[service.name] = "not_connected" if connection is None else "needs_attention"
        await self.db.start_effect(conn, execution_key=key, operation=CONNECTIONS)
        await self.db.complete_effect(
            conn, execution_key=key, result={"version": 1, "connections": states}
        )
        return states

    async def call(self, *, conn, run, workflow, spec, payload):
        try:
            if not isinstance(payload, dict) or set(payload) != {
                "service",
                "step",
                "operation",
                "arguments",
            }:
                raise ValueError
            step = payload["step"]
            if not isinstance(step, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", step
            ):
                raise ValueError
            service = next(s for s in spec.services if s.name == payload["service"])
            custom = bool(CUSTOM_KEY.fullmatch(service.provider_key))
            managed = managed_services.is_managed(service.provider_key)
            paid = managed and managed_services.paid(service.provider_key)
            args = payload["arguments"]
            if custom:
                if payload["operation"] != "http.request":
                    raise ValueError
                request_contract(args)
                capability = "http.read" if args["method"] in READ_METHODS else "http.write"
            else:
                capability, fields = OPERATIONS[(service.provider_key, payload["operation"])]
                if not isinstance(args, dict) or set(args) - fields:
                    raise ValueError
                check = ARGUMENT_CHECKS.get(service.provider_key)
                if check is not None:
                    check(payload["operation"], args)
            if (
                capability not in service.capabilities
                or len(json.dumps(args, allow_nan=False).encode()) > 16_000
            ):
                raise ValueError
        except ServiceArgumentError as exc:
            raise CodeServiceError(
                f"The service request differs from its declared contract: {exc}."
            ) from None
        except (ValueError, TypeError, KeyError, StopIteration, RecursionError):
            raise CodeServiceError(
                "The service request differs from its declared contract."
            ) from None
        if service in optional_services(workflow, spec):
            saved = await self.db.get_effect(_connections_key(run), conn=conn)
            pinned = ((saved.result or {}).get("connections") or {}) if saved else {}
            if pinned.get(service.name) != "connected":
                raise CodeServiceError(
                    f"The optional {service.name} connection is not available to this run; "
                    "no request was made.",
                    code="not_connected",
                )
        fingerprint = digest(
            {
                "definition": run.definition_commit_sha,
                "service": asdict(service),
                "request": payload,
            }
        )
        prefix = f"{run.id}:code-service:"
        key = prefix + digest(step)
        async with self.db.effect_lock(f"{run.id}:code-service-gate", OPERATION, conn=conn):
            from tin_lite.code_models import CodeModelError

            try:
                # A paid read needs the run's reserved credit budget, as a model call does.
                await self.authorize(conn=conn, run=run, workflow=workflow, require_budget=paid)
            except CodeModelError as exc:
                if exc.code == "model_spending_stopped":
                    raise CodeServiceError(
                        "This run has no authorized spending for a paid read; no request was made."
                    ) from None
                raise CodeServiceError(
                    "The run no longer has permission to use services.", fatal=True
                ) from None
            connection = None
            if managed:
                if not managed_services.configured(self.managed.settings, service.provider_key):
                    raise CodeServiceError(managed_services.not_configured(service.provider_key))
                # No founder account to pin: Tin's own key serves every project the same way.
                binding = digest({"managed": service.provider_key})
            else:
                requirement = IntegrationRequirement(service.provider_key, service.capabilities)
                try:
                    await self.integrations.ensure_requirements(
                        project_id=run.project_id, requirements=(requirement,)
                    )
                    connection = await self.db.get_integration_connection(
                        project_id=run.project_id, provider_key=service.provider_key
                    )
                    if connection is None:
                        raise IntegrationError("connection removed")
                except IntegrationError:
                    raise CodeServiceError(
                        "The declared project connection is unavailable or permission was removed."
                    ) from None
                # Pin selected account/origin/permissions for this run; keys may still rotate.
                binding = digest(
                    {
                        "id": str(connection.id),
                        "account": connection.external_account_id,
                        "configuration": {
                            k: v
                            for k, v in connection.configuration.items()
                            if k != "access_verified"
                        },
                    }
                )
            prior = await conn.fetchval(
                """SELECT result->>'binding' FROM effect_receipts WHERE operation=$1
                   AND execution_key LIKE $2 AND result->>'service'=$3 LIMIT 1""",
                OPERATION,
                prefix + "%",
                service.name,
            )
            if prior is not None and prior != binding:
                raise CodeServiceError("The connection changed during this run. Start a new run.")
            saved = await self.db.get_effect(key, conn=conn)
            if saved:
                record = saved.result or {}
                if record.get("fingerprint") != fingerprint:
                    raise CodeServiceError("This service step already has a different request.")
                if saved.status == "completed":
                    if record.get("error") == "response_too_large":
                        raise _too_large(service)
                    if record.get("error"):
                        raise CodeServiceError(
                            record.get("message") or "The provider refused it.",
                            code=record["error"],
                            provider_error=record.get("provider_error"),
                        )
                    return record["response"]
                raise CodeServiceError(
                    "A service request has an unconfirmed result; "
                    "Tin will not repeat it automatically."
                )
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM effect_receipts WHERE operation=$1 "
                "AND execution_key LIKE $2 AND status <> 'completed')",
                OPERATION,
                prefix + "%",
            ):
                raise CodeServiceError(
                    "An earlier service request is unresolved; a new step cannot retry it."
                )
            count = await conn.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation=$1 "
                "AND execution_key LIKE $2 AND result->>'service'=$3",
                OPERATION,
                prefix + "%",
                service.name,
            )
            if count >= service.max_calls:
                raise CodeServiceError("The workflow reached its declared service-call limit.")
            secret = None
            if custom:
                if args["method"] not in connection.configuration["methods"]:
                    raise CodeServiceError("The connection does not allow this method.")
                secret, secret_revision = await self.integrations.custom.open_secret_record(
                    run.project_id, connection.configuration["secret_name"]
                )
            record = {
                "version": 1,
                "run_id": str(run.id),
                "step": step,
                "service": service.name,
                "provider": service.provider_key,
                "binding": binding,
                "fingerprint": fingerprint,
            }
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            await self.db.save_effect_progress(conn, execution_key=key, result=record)
            # Customer-provider usage is separate from Tin purchases. These observations
            # deliberately do not enter billing.begin_operation or settle customer credits.
            # A free managed read is Tin's own tool call at a published price of $0. A paid
            # one is observed by its adapter instead, which reserves and settles its cost.
            usage_key = None if paid else f"usage:{key}"
            usage = {
                "version": 1,
                "run_id": str(run.id),
                "provider": managed_services.USAGE_PROVIDER.get(
                    service.provider_key, service.provider_key
                ),
                "category": "tool" if managed else "connected_api",
                "endpoint": payload["operation"],
                "step": step,
                "attempted_at": datetime.now(UTC).isoformat(),
                "outcome": "unconfirmed",
                "usage": None,
                "reported_cost_usd": None,
            }
            received = {
                **usage,
                "outcome": "response_received",
                "usage": {"requests": 1},
                **({"reported_cost_usd": "0"} if managed else {}),
            }
            if usage_key:
                await self.db.start_effect(
                    conn, execution_key=usage_key, operation="external_usage_v1"
                )
                await self.db.save_effect_progress(conn, execution_key=usage_key, result=usage)
            try:
                async with asyncio.timeout(CALL_SECONDS):
                    response = (
                        await request_api(
                            connection,
                            secret,
                            args,
                            maximum=service.max_response_bytes,
                            operation_id=key,
                            client=self.client,
                            resolver=self.resolver,
                        )
                        if custom
                        else await self._managed(conn, run, service, payload, key)
                        if managed
                        else await self.adapter(
                            service.provider_key,
                            payload["operation"],
                            args,
                            run,
                            connection,
                            key,
                            max_response_bytes=service.max_response_bytes,
                        )
                    )
                if (
                    len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
                    > service.max_response_bytes
                ):
                    raise ServiceResponseTooLarge("oversized response")
            except ServiceResponseTooLarge:
                # A response arrived and was refused by size: a known outcome, not an uncertain
                # one. Settle both receipts so later steps are not blocked; the call still counts.
                async with conn.transaction():
                    await self.db.complete_effect(
                        conn, execution_key=key, result={**record, "error": "response_too_large"}
                    )
                    if usage_key:
                        await self.db.complete_effect(
                            conn, execution_key=usage_key, result=received
                        )
                raise _too_large(service) from None
            except BillingError:
                # Credits or project limits refused the reservation before anything was sent.
                async with conn.transaction():
                    await self.db.complete_effect(
                        conn,
                        execution_key=key,
                        result={**record, "error": "spending_stopped", "message": SPENDING_STOPPED},
                    )
                raise CodeServiceError(SPENDING_STOPPED, code="spending_stopped") from None
            except (ServiceCallRefused, IntegrationProviderError) as exc:
                # The provider answered and refused (rate limit, missing permission, revoked
                # key, a query it could not run): settle the step so a new step may try again.
                # Every gateway operation that reaches a provider error status is a read.
                # Keep what the provider said: without it an author debugs blind.
                detail = exc.provider_error
                refused = {
                    **record,
                    "error": exc.code if isinstance(exc, ServiceCallRefused) else "provider_error",
                    "message": explain(str(exc), detail)[:MESSAGE_LIMIT],
                }
                if detail is not None:
                    refused["provider_error"] = detail.to_dict()
                async with conn.transaction():
                    await self.db.complete_effect(conn, execution_key=key, result=refused)
                    if usage_key:
                        await self.db.complete_effect(
                            conn, execution_key=usage_key, result=received
                        )
                    # A rejected credential needs attention whatever body the API sent with it.
                    if isinstance(exc, InvalidAPIResponse) and exc.status in {401, 403}:
                        await self._authentication_failed(conn, run, connection, secret_revision)
                logger.info(
                    "service call refused",
                    extra={
                        "run_id": str(run.id),
                        "service": service.name,
                        "step": step,
                        "provider": service.provider_key,
                        "error_code": refused["error"],
                        "provider_status": detail.status if detail else None,
                        "provider_error_type": detail.type if detail else None,
                        "provider_error_code": detail.code if detail else None,
                    },
                )
                raise CodeServiceError(
                    refused["message"],
                    code=refused["error"],
                    provider_error=refused.get("provider_error"),
                ) from None
            except (
                IntegrationError,
                httpx.HTTPError,
                OSError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
            ):
                raise CodeServiceError(
                    "Service response unavailable or invalid; "
                    "the request will not be repeated automatically."
                ) from None
            async with conn.transaction():
                from tin_lite.code_progress import project_completed_steps

                await self.db.complete_effect(
                    conn, execution_key=key, result={**record, "response": response}
                )
                if usage_key:
                    await self.db.complete_effect(conn, execution_key=usage_key, result=received)
                await project_completed_steps(conn, run.id)
                if custom and response["status"] in {401, 403}:
                    await self._authentication_failed(conn, run, connection, secret_revision)
                if custom and 200 <= response["status"] < 300:
                    await conn.execute(
                        """UPDATE integration_connections
                           SET configuration=jsonb_set(configuration,'{access_verified}','true'),
                           last_checked_at=now() WHERE id=$1 AND configuration->>'revision'=$2
                           AND EXISTS(SELECT 1 FROM project_secrets WHERE project_id=$3
                             AND name=$4 AND revision=$5)""",
                        connection.id,
                        connection.configuration["revision"],
                        run.project_id,
                        connection.configuration["secret_name"],
                        secret_revision,
                    )
            return response

    async def _authentication_failed(self, conn, run, connection, secret_revision):
        await conn.execute(
            """UPDATE integration_connections
               SET status='needs_attention', last_error_code='authentication_failed',
                   configuration=jsonb_set(configuration,'{access_verified}','false'),
                   last_checked_at=now()
               WHERE id=$1 AND configuration->>'revision'=$2
               AND EXISTS(SELECT 1 FROM project_secrets WHERE project_id=$3
                 AND name=$4 AND revision=$5)""",
            connection.id,
            connection.configuration["revision"],
            run.project_id,
            connection.configuration["secret_name"],
            secret_revision,
        )

    async def _managed(self, conn, run, service, payload, key):
        from tin_lite.usage_capture import external_usage_scope

        # A paid read's observation reserves this ceiling before dispatch and settles the
        # reported cost through service_pricing, as native DataForSEO calls do.
        ceiling = managed_services.CALL_CEILING_USD.get(service.provider_key)
        with external_usage_scope(
            self.db, conn, run.id, f"service:{payload['step']}", maximum_usd=ceiling
        ):
            return await self.managed.call(
                service.provider_key,
                payload["operation"],
                payload["arguments"],
                execution_key=key,
                max_response_bytes=service.max_response_bytes,
            )

    async def adapter(self, provider, operation, args, run, connection, key, *, max_response_bytes):
        service = self.integrations
        if provider in {STRIPE_PROVIDER, POSTHOG_PROVIDER}:
            adapter = service.stripe if provider == STRIPE_PROVIDER else service.posthog
            return await adapter.call(
                operation,
                args,
                connection=connection,
                run_id=run.id,
                execution_key=key,
                max_response_bytes=max_response_bytes,
            )
        if operation == "sites.list":
            return {
                "sites": [asdict(x) for x in await service.google_sites(project_id=run.project_id)]
            }
        if operation == "repositories.list":
            return {
                "repositories": [
                    asdict(x) for x in await service.github_repositories(project_id=run.project_id)
                ]
            }
        if provider == "analytics.gsc":
            return await service.search_console_analytics(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=key,
                expected_site_url=connection.configuration.get("selected_site_url"),
                max_response_bytes=max_response_bytes,
                **args,
            )
        common = {
            "project_id": run.project_id,
            "run_id": run.id,
            "connection_id": connection.id,
            "external_account_id": connection.external_account_id,
            "execution_key": key,
        }
        if operation == "gmail.messages.search":
            return await service.workspace_search_messages(**common, **args)
        if operation == "gmail.thread.read":
            return await service.workspace_get_thread(**common, **args)
        return await service.workspace_list_calendar_events(**common, **args)
