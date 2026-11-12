"""Safe class-and-stage labels for an owner-consent sync stop.

Adapted from northstar-trading@8256c16c ``tradingview/auth_safety.py:82-199``:
``_CLASS_LABELS`` keeps the exact type→literal map, ``_class_label`` and
``_leaf`` keep the bounded group walk. The stage order comes from the installed
MCP SDK's OAuth flow (``mcp/client/auth/oauth2.py:385`` authorize/callback,
``:453`` token exchange, ``:604`` the flow that starts it) and the session it
then enters. Only fixed literals leave this module; never an exception argument,
URL, token, or upstream message.
"""

from __future__ import annotations

import re

import httpx2
from mcp.client.auth.exceptions import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.streamable_http import ResumptionError, StreamableHTTPError
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from sextant.tradingview.sync import SyncError

MAX_GROUP_DEPTH = 32
MAX_GROUP_NODES = 256
MAX_DETAIL = 240
MAX_NAMES = 8

# RFC 6749 section 5.2's fixed token-error vocabulary; names only, never the body.
_OAUTH_CODES = (
    "invalid_request",
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
)
_OAUTH_CODE = re.compile(r"(?<![A-Za-z0-9_.-])(" + "|".join(_OAUTH_CODES) + r")(?![A-Za-z0-9_.-])")
_TOKEN_STATUS = re.compile(r"^Token exchange failed \((\d{3})\)")
_ENDPOINTS = (
    ("/mcp/oauth/token", "token"),
    ("/mcp/oauth/register", "registration"),
    ("/mcp", "session"),
)
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

_CLASS_LABELS: dict[type[BaseException], str] = {
    SyncError: "SyncError",
    OAuthTokenError: "OAuthTokenError",
    OAuthRegistrationError: "OAuthRegistrationError",
    OAuthFlowError: "OAuthFlowError",
    ResumptionError: "ResumptionError",
    StreamableHTTPError: "StreamableHTTPError",
    MCPError: "MCPError",
    httpx2.ConnectTimeout: "ConnectTimeout",
    httpx2.ReadTimeout: "ReadTimeout",
    httpx2.WriteTimeout: "WriteTimeout",
    httpx2.PoolTimeout: "PoolTimeout",
    httpx2.TimeoutException: "TimeoutException",
    httpx2.ConnectError: "ConnectError",
    httpx2.ReadError: "ReadError",
    httpx2.WriteError: "WriteError",
    httpx2.CloseError: "CloseError",
    httpx2.NetworkError: "NetworkError",
    httpx2.ProxyError: "ProxyError",
    httpx2.UnsupportedProtocol: "UnsupportedProtocol",
    httpx2.LocalProtocolError: "LocalProtocolError",
    httpx2.RemoteProtocolError: "RemoteProtocolError",
    httpx2.TransportError: "TransportError",
    httpx2.DecodingError: "DecodingError",
    httpx2.TooManyRedirects: "TooManyRedirects",
    httpx2.HTTPStatusError: "HTTPStatusError",
    httpx2.RequestError: "RequestError",
    TimeoutError: "TimeoutError",
    ConnectionError: "ConnectionError",
    OSError: "OSError",
    ValueError: "ValueError",
    RuntimeError: "RuntimeError",
}

# OAuthTokenError and OAuthRegistrationError subclass OAuthFlowError; order matters.
_STAGE_BY_CLASS: tuple[tuple[type[BaseException], str], ...] = (
    (OAuthRegistrationError, "client registration"),
    (OAuthTokenError, "token"),
    (OAuthFlowError, "callback"),
)


def class_label(error: BaseException) -> str:
    """The exact type's fixed label, or ExceptionGroup, never its text."""
    if type(error) is ExceptionGroup:
        return "ExceptionGroup"
    if type(error) is BaseExceptionGroup:
        return "BaseExceptionGroup"
    return _CLASS_LABELS.get(type(error), "UnrecognizedError")


def _leaf(error: BaseException) -> BaseException:
    """Bound the group walk by work AND depth, reading each group's children once."""
    seen: set[int] = set()
    for _ in range(min(MAX_GROUP_DEPTH, MAX_GROUP_NODES)):
        if not isinstance(error, BaseExceptionGroup) or id(error) in seen:
            break
        seen.add(id(error))
        children = error.exceptions
        if type(children) is not tuple or not children:
            break
        if not isinstance(children[0], BaseException):
            break
        error = children[0]
    return error


def stage(error: BaseException) -> str:
    """The flow position implied by the SDK's order: registration, callback, token, session."""
    leaf = _leaf(error)
    if isinstance(leaf, SyncError) and leaf.stage:
        # A local refusal already knows exactly where it is; keep its fixed literal.
        return leaf.stage
    for kind, name in _STAGE_BY_CLASS:
        if isinstance(leaf, kind):
            return name
    return "session"


def leaf_label(error: BaseException) -> str:
    """The fixed label of the group's first leaf; the class a stop is caused by."""
    return class_label(_leaf(error))


def _causes(error: BaseException):
    """Bounded cause walk; group members are already reached through _leaf."""
    for _ in range(MAX_GROUP_DEPTH):
        yield error
        nxt = error.__cause__ if error.__cause__ is not None else error.__context__
        if nxt is None or nxt is error:
            return
        error = nxt


def _status_and_endpoint(error: BaseException) -> tuple[int | None, str]:
    for item in _causes(error):
        if isinstance(item, httpx2.HTTPStatusError):
            status = getattr(item.response, "status_code", None)
            if not isinstance(status, int) or not 100 <= status <= 599:
                continue
            path = getattr(getattr(item.request, "url", None), "path", "")
            endpoint = next((name for prefix, name in _ENDPOINTS if path == prefix), "")
            return status, endpoint
        if isinstance(item, OAuthTokenError):
            text = item.args[0] if item.args and isinstance(item.args[0], str) else ""
            match = _TOKEN_STATUS.match(text)
            if match and 100 <= int(match.group(1)) <= 599:
                return int(match.group(1)), "token"
    return None, ""


def _oauth_code(error: BaseException) -> str:
    for item in _causes(error):
        if isinstance(item, OAuthTokenError):
            text = item.args[0] if item.args and isinstance(item.args[0], str) else ""
            match = _OAUTH_CODE.search(text)
            if match:
                return match.group(1)
    return ""


def _field_names(error: BaseException) -> list[str]:
    for item in _causes(error):
        if isinstance(item, ValidationError):
            names: list[str] = []
            for detail in item.errors():
                for part in detail.get("loc", ()):
                    if isinstance(part, str) and part != "body" and _FIELD_NAME.match(part):
                        if part not in names:
                            names.append(part)
            return sorted(names)[:MAX_NAMES]
    return []


def detail(error: BaseException) -> str:
    """Fixed-vocabulary, names-only detail: status, endpoint class, OAuth code, field names."""
    bits: list[str] = []
    leaf = _leaf(error)
    status, endpoint = _status_and_endpoint(leaf)
    if status is not None:
        bits.append(f"HTTP {status}" + (f" at {endpoint} endpoint" if endpoint else ""))
    code = _oauth_code(leaf)
    if code:
        bits.append(f"OAuth error {code}")
    fields = _field_names(leaf)
    if fields:
        bits.append("fields " + ", ".join(fields))
    return "; ".join(bits)[:MAX_DETAIL]
