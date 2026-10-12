"""Attach a founder-safe classification to every failing activity.

The activity's exception is converted exactly as Temporal would convert it (same type name,
message and retryability, so retry policies and `non_retryable_error_types` are unchanged),
plus one small details item from `failures.classify`. The workflow then reads what the
founder may see without guessing from text.
"""

from __future__ import annotations

from typing import Any

from temporalio.exceptions import ApplicationError, FailureError
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from tin_lite.failures import classify, from_details


class FailureClassificationInterceptor(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _ClassifyingActivity(next)


class _ClassifyingActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        try:
            return await super().execute_activity(input)
        except ApplicationError as exc:
            if exc.details or from_details(exc) is not None:
                raise
            failure = classify(exc)
            raise ApplicationError(
                exc.message,
                failure.details(),
                type=exc.type,
                non_retryable=exc.non_retryable,
                next_retry_delay=exc.next_retry_delay,
                category=exc.category,
            ) from exc.__cause__
        except FailureError:
            raise
        except Exception as exc:
            failure = classify(exc)
            raise ApplicationError(
                str(exc),
                failure.details(),
                type=type(exc).__name__,
            ) from exc
