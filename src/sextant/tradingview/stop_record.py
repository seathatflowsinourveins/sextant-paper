"""LEAD-1904 stop records over the existing guard, never upstream content.

MCP 2.3.0 mcp_types.methods supplies the method vocabulary; account.py at
bbbe7f5 supplies the unchanged refusal predicates and tool-schema exclusions.
"""

import asyncio
import json
import sys

import httpx2
from mcp_types.methods import MONOLITH_NOTIFICATIONS, MONOLITH_REQUESTS

from sextant.execution.state import write_atomic


def refusal_rule(value):
    """First matching rule, with exactly the guard's existing truth conditions."""
    if isinstance(value, dict):
        if value.get("isError") or value.get("error") or value.get("success") is False:
            return "is_error"
        text = value.get("text")
        if isinstance(text, str) and any(word in text.casefold() for word in ("quota", "suspend")):
            return "quota_text"
        tool = isinstance(value.get("name"), str) and isinstance(value.get("inputSchema"), dict)
        values = (
            v for k, v in value.items() if not (tool and k in {"inputSchema", "outputSchema"})
        )
    elif isinstance(value, list):
        values = iter(value)
    else:
        return None
    return next((rule for item in values if (rule := refusal_rule(item))), None)


def exception_rule(error):
    # Runtime imports avoid account -> diagnostics -> sync -> account at import time.
    from sextant.tradingview.account import AccountError
    from sextant.tradingview.diagnostics import class_label

    if isinstance(error, AccountError) and error.rule:
        return error.rule
    if type(error) is AccountError:
        return "AccountError"
    if type(error) is json.JSONDecodeError:
        return "JSONDecodeError"
    if type(error) is asyncio.CancelledError:
        return "CancelledError"
    return class_label(error) if error is not None else "UnfinishedRequest"


def record_request(
    path, state, *, at, ordinal, rule, request=None, status=None, method=None, action="stop"
):
    """Persist with the outcome, then print the same fixed-field record on one line."""
    http_method = None
    if request is not None:
        http_method = (
            request.method
            if request.method
            in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
            else "unknown"
        )
        try:
            body = json.loads(request.content) if request.method == "POST" else {}
        except (ValueError, httpx2.RequestNotRead):
            body = {}
        method = body.get("method") if isinstance(body, dict) else None
    record = {
        "action": action,
        "at": at,
        "http_method": http_method,
        "status_code": status,
        "rpc_method": (
            method
            if isinstance(method, str)
            and (method in MONOLITH_REQUESTS or method in MONOLITH_NOTIFICATIONS)
            else None
            if method is None
            else "unknown"
        ),
        "rule": rule,
        "request_ordinal": ordinal,
    }
    state["audit"].append(record)
    write_atomic(path, state)
    message = (
        "TradingView account stopped; lead review required before further calls."
        if action == "stop"
        else "TradingView optional GET stream unavailable."
    )
    print(message + " " + action + "=" + json.dumps(record, separators=(",", ":")), file=sys.stderr)
