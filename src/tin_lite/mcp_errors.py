"""One error boundary for every Tin MCP tool: anticipated failures reach the agent as ToolError.

The MCP SDK forwards a message to the client only for ``ToolError``; any other exception is
reported as a bare ``Error executing tool <name>`` with the text kept server-side. Tin's tool
bodies and services raise ``LookupError``, ``ValueError``, ``PermissionError`` and
``RuntimeError`` subclasses for failures the agent is meant to read and act on, so
``GuardedMCPServer`` wraps every registered tool and translates those. Programming errors
(``TypeError``, ``AttributeError``, ``AssertionError``, driver and transport errors) stay masked,
and so does the text of a provider, sandbox or Temporal ``RuntimeError`` (failures.py).
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from tin_lite.failures import public_error_text

BOUNDARY_MARKER = "__tin_mcp_boundary__"

# Same order and meaning as the REST mapping in api.py: forbidden, not found, invalid input,
# conflict with current state. Anything outside these is a crash.
_PREFIXES: tuple[tuple[type[BaseException], str], ...] = (
    (PermissionError, "forbidden"),
    (LookupError, "not_found"),
    (ValueError, "invalid"),
    (RuntimeError, "conflict"),
)


def tool_error(exc: BaseException) -> ToolError | None:
    """The ToolError an agent can act on, or None when the exception is a crash."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, (KeyError, IndexError)):
        return None
    diagnostic = getattr(exc, "diagnostic", None)
    if callable(diagnostic):
        return ToolError(json.dumps(diagnostic()))
    if isinstance(exc, ValidationError):
        return ToolError(f"invalid: {exc}")
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return ToolError(f"{code}: {exc}")
    for cls, prefix in _PREFIXES:
        if isinstance(exc, cls):
            # Input and lookup errors help the calling agent fix its call. A RuntimeError from
            # a provider, sandbox or Temporal client describes Tin's internals instead.
            text = public_error_text(exc) if cls is RuntimeError else str(exc)
            return ToolError(f"{prefix}: {text}")
    return None


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an async tool body so anticipated failures become ToolError; crashes pass through."""
    if getattr(fn, BOUNDARY_MARKER, False):
        return fn
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"Tin MCP tools must be async: {fn.__qualname__}")

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        try:
            return await fn(**kwargs)
        except ToolError:
            raise
        except Exception as exc:
            translated = tool_error(exc)
            if translated is None:
                raise
            raise translated from exc

    setattr(wrapper, BOUNDARY_MARKER, True)
    return wrapper


class GuardedMCPServer(MCPServer):
    """An MCPServer whose every tool runs behind ``guarded``."""

    def add_tool(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().add_tool(guarded(fn), *args, **kwargs)
