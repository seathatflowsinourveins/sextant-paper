"""Official MCP membership/readback slice from northstar-trading@8256c16c.

Adapted from tradingview/{research,sync,contracts}.py. The SDK session API sends
each write once; Client.call_tool may re-send on HEADER_MISMATCH, so is excluded.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps
from typing import Any, Never

from jsonschema import Draft202012Validator
from mcp import Client
from mcp.shared.exceptions import MCPError

from sextant.tradingview.account import Account, AccountError, MeteredClient
from sextant.tradingview.inputs import NAMES, ResearchInputs

ALLOWED = {
    "list_watchlists": set(),
    "get_watchlist": {"watchlist_id"},
    "create_watchlist": {"name", "symbols"},
    "add_to_watchlist": {"watchlist_id", "symbols"},
    "remove_from_watchlist": {"watchlist_id", "symbols"},
}
WIRE = {"mcp-watchlist-" + name.replace("_", "-"): name for name in ALLOWED}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

# Fixed refusal literals. The CLI may echo these exact strings and nothing else;
# SAFE_STOP_MESSAGES is built from the constants, never hand-copied.
INCOMPLETE_RESPONSE = "Incomplete or failed MCP response; no automatic replay"
AMBIGUOUS_TOOL_IDENTITY = "Ambiguous official tool identity"
REPEATED_TOOLS_CURSOR = "Repeated tools/list cursor"
INCOMPLETE_TOOL_DISCOVERY = "Incomplete tool discovery"
TOOLS_UNAVAILABLE = "Required official watchlist tools unavailable"
RESEARCH_EXPIRED = "Research expired during sync; no further write"
NON_DISPLAY_INPUT = "Non-display operation or incompatible official input schema"
SESSION_REFUSED = "MCP session refused or interrupted; no automatic replay"
CALL_FAILED = "Official MCP call failed or was quota-limited; re-inventory before retry"
UNKNOWN_RESULT_ENVELOPE = "Unknown MCP result envelope"
INVALID_LIST_ID = "Invalid or conflicting owned-list ID"
LIST_READ_ONLY = "Owned watchlist is read-only or changed identity"
UNKNOWN_MEMBERSHIP_ENVELOPE = "Unknown watchlist membership envelope"
UNKNOWN_INVENTORY_ENVELOPE = "Unknown watchlist inventory envelope"
DUPLICATE_LIST_NAME = "Duplicate reserved watchlist name"
LIST_ALIAS = "Owned watchlist aliases another list"
INVALID_INPUTS = "Only a validated nonempty research membership pair can be synced"
AMBIGUOUS_MUTATION = "Ambiguous watchlist mutation; re-inventory before retry"
UNKNOWN_CREATE_RESULT = "Unknown create result; re-inventory before retry"
CREATE_ALIAS = "Created list aliases an existing list; no further writes"
CREATE_MISSING = "Created watchlist missing from readback"
AMBIGUOUS_REMOVAL = "Ambiguous corrective removal; no replay"
READBACK_MISMATCH = "Readback mismatch after one corrective removal; no replay"
REJECTED_TOO_MANY = "Readback rejected more than five distinct names; verified subsets retained"
# The stored sign-in's stops (--keep-login, D-23). Each names the one owner step.
LOGIN_REFUSED = (
    "TradingView refused the stored sign-in and it was deleted; the owner runs "
    "--authorize --keep-login"
)
LOGIN_UNAVAILABLE = (
    "TradingView's sign-in service was unavailable; the stored sign-in is unchanged "
    "and the next run retries it"
)
LOGIN_UNUSABLE = (
    "TradingView's answer did not refresh the stored sign-in, which is unchanged; if this "
    "repeats, the owner runs --forget-login, then --authorize --keep-login"
)
LOGIN_NEEDS_CONSENT = (
    "No usable stored sign-in and no browser without --authorize; the owner runs "
    "--authorize --keep-login"
)
LOGIN_UNREADABLE = (
    "The stored sign-in cannot be read and is unchanged; the owner runs "
    "--forget-login, then --authorize --keep-login"
)
LOGIN_STORE_UNAVAILABLE = "The encrypted sign-in store is unavailable; nothing was changed"
LOGIN_STOPS = (
    LOGIN_REFUSED,
    LOGIN_UNAVAILABLE,
    LOGIN_UNUSABLE,
    LOGIN_NEEDS_CONSENT,
    LOGIN_UNREADABLE,
    LOGIN_STORE_UNAVAILABLE,
)
SAFE_STOP_MESSAGES = (
    INCOMPLETE_RESPONSE,
    AMBIGUOUS_TOOL_IDENTITY,
    REPEATED_TOOLS_CURSOR,
    INCOMPLETE_TOOL_DISCOVERY,
    TOOLS_UNAVAILABLE,
    RESEARCH_EXPIRED,
    NON_DISPLAY_INPUT,
    SESSION_REFUSED,
    CALL_FAILED,
    UNKNOWN_RESULT_ENVELOPE,
    INVALID_LIST_ID,
    LIST_READ_ONLY,
    UNKNOWN_MEMBERSHIP_ENVELOPE,
    UNKNOWN_INVENTORY_ENVELOPE,
    DUPLICATE_LIST_NAME,
    LIST_ALIAS,
    INVALID_INPUTS,
    AMBIGUOUS_MUTATION,
    UNKNOWN_CREATE_RESULT,
    CREATE_ALIAS,
    CREATE_MISSING,
    AMBIGUOUS_REMOVAL,
    READBACK_MISMATCH,
    REJECTED_TOO_MANY,
    *LOGIN_STOPS,
)

_JSON_TYPES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
    Decimal: "number",
    type(None): "null",
}
_ERROR_WORDS = ("quota", "rate", "plan", "permission", "invalid", "unavailable")
_ERROR_WORD = re.compile(r"\b(?:" + "|".join(_ERROR_WORDS) + r")\b", re.IGNORECASE)


def _shape(value: Any) -> str:
    """JSON type and key/item count only; mapping keys can name owner lists."""
    if isinstance(value, dict):
        return f"JSON object, {len(value)} keys"
    if isinstance(value, list):
        return f"JSON array, {len(value)} items"
    return f"JSON {_JSON_TYPES.get(type(value), 'unknown')}"


def _error_word(result: Any) -> str:
    """One fixed word matched in the result text; the text itself is never emitted."""
    text = " ".join(
        part.text for part in result.content if part.type == "text" and isinstance(part.text, str)
    )
    match = _ERROR_WORD.search(text)
    return match.group(0).lower() if match else "unclassified"


class SyncError(RuntimeError):
    """Re-inventory after refusal: an earlier write may have succeeded.

    ``label``, ``stage`` and ``detail`` are fixed diagnostic literals (a type
    name, one of the flow positions: inputs, authorize, callback, token, client
    registration, session, tool list, tool call, apply, and fixed-vocabulary
    names-only detail). They never carry upstream text, values or credentials.
    """

    def __init__(self, message: str, *, label: str = "", stage: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.label = label
        self.stage = stage
        self.detail = detail

    def stop_tail(self) -> str:
        tail = f": {self}" if str(self) in SAFE_STOP_MESSAGES else ""
        tail += f" ({self.detail})" if self.detail else ""
        return f"{self.label or type(self).__name__} at {self.stage or 'sync'}{tail}"


def _sanitized(error: BaseException) -> Never:
    """Upstream auth_safety.raise_sanitized; no retained raw exception graph."""
    try:
        raise error from None
    except BaseException:
        error.__context__ = None
        error.__cause__ = None
        raise


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        if (
            value.get("success") is False
            or value.get("error")
            or value.get("partial") is True
            or value.get("has_more") is True
            or value.get("next_cursor")
        ):
            raise SyncError(
                INCOMPLETE_RESPONSE,
                stage="tool call",
                detail=_shape(value),
            )
        if set(value) == {"result"}:
            return _unwrap(value["result"])
        if set(value) == {"data", "success"} and value["success"] is True:
            return _unwrap(value["data"])
    return value


_WRITE_KEYS = frozenset({"watchlist_id", "symbols"})
_ENVELOPE_KEYS = {
    frozenset({"success"}): "success",
    frozenset({"data", "success"}): "data,success",
    frozenset({"watchlist"}): "watchlist",
    frozenset({"watchlist", "note"}): "watchlist,note",
    frozenset({"watchlists"}): "watchlists",
    frozenset({"partial", "watchlists"}): "partial,watchlists",
    frozenset({"result"}): "result",
}
_UNPARSED = object()


def _type_word(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return _JSON_TYPES.get(type(value), "unknown")


def _array_item(items: list[Any]) -> str:
    kinds = {_type_word(item) for item in items}
    if not kinds:
        return ""
    return kinds.pop() if len(kinds) == 1 else "mixed"


def _write_shape(value: Any, carrier: str) -> str:
    """Fixed name for a write result: known keys and JSON types, no value or count.

    ``carrier`` is the pinned SDK's result field that carried the value
    (``structured_content`` or ``content``); unknown keys collapse to ``extra``
    or ``unknown`` so an owner list name never reaches the stop tail.
    """
    if isinstance(value, dict):
        keys = frozenset(value)
        present = [key for key in ("watchlist_id", "symbols") if key in keys]
        if not keys:
            name = ""
        elif present:
            name = ",".join(present) + (",extra" if keys - _WRITE_KEYS else "")
        else:
            name = _ENVELOPE_KEYS.get(keys, "unknown")
        fields = []
        for key in present:
            if isinstance(value[key], list):
                fields.append(f"{key}:array[{_array_item(value[key])}]")
            else:
                fields.append(f"{key}:{_type_word(value[key])}")
        shape = f"object{{{name}}}"
        if fields:
            shape += f"[{'|'.join(fields)}]"
        return f"{carrier}:{shape}"
    return f"{carrier}:{_type_word(value)}"


def _documented_write(value: Any) -> bool:
    """Exactly the measured write envelope; anything wider is still ambiguous."""
    return (
        isinstance(value, dict)
        and set(value) == _WRITE_KEYS
        and isinstance(value["watchlist_id"], str)
        and isinstance(value["symbols"], list)
        and all(isinstance(symbol, str) for symbol in value["symbols"])
    )


def _account_guard(method):
    @wraps(method)
    async def run(self, *args, **kwargs):
        if method.__name__ in {"call", "call_raw"} and args[0] not in ALLOWED:
            raise SyncError(NON_DISPLAY_INPUT, stage="tool call")
        try:
            with self.account.locked():
                try:
                    return await method(self, *args, **kwargs)
                except BaseException:
                    self.account.stop_day()
                    raise
        except AccountError:
            raise SyncError(SESSION_REFUSED, stage="account guard") from None

    return run


class OfficialTools:
    """Exact display allowlist, discovered schemas, serial pacing and safe errors."""

    def __init__(
        self,
        client: Client,
        interval: float | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.client, self.now = client, now
        self.account = Account()
        self.schemas: dict[str, Any] = {}
        self.wire: dict[str, str] = {}
        self.lock = asyncio.Lock()

    @_account_guard
    async def discover(self) -> None:
        self.schemas.clear()
        self.wire.clear()
        cursor, seen = None, set()
        offered: set[str] = set()
        for _ in range(100):
            async with self.lock:
                page = await self._request(
                    lambda cursor=cursor: self.client.list_tools(
                        cursor=cursor, cache_mode="refresh"
                    )
                )
            for tool in page.tools:
                if isinstance(tool.name, str) and _NAME.match(tool.name):
                    offered.add(tool.name)
                name = WIRE.get(tool.name, tool.name)
                if name in ALLOWED:
                    if name in self.schemas:
                        raise SyncError(AMBIGUOUS_TOOL_IDENTITY, stage="tool list")
                    self.schemas[name], self.wire[name] = tool.input_schema, tool.name
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in seen:
                raise SyncError(REPEATED_TOOLS_CURSOR, stage="tool list")
            seen.add(cursor)
        else:
            raise SyncError(INCOMPLETE_TOOL_DISCOVERY, stage="tool list")
        if set(self.schemas) != set(ALLOWED):
            required = set(WIRE)
            missing = sorted(required - set(self.wire.values()))[:8]
            extra = sorted(raw for raw in offered if WIRE.get(raw, raw) not in ALLOWED)[:8]
            detail = (
                f"tool list extra {', '.join(extra) or 'none'}; "
                f"missing {', '.join(missing) or 'none'}"
            )
            raise SyncError(
                TOOLS_UNAVAILABLE,
                stage="tool list",
                detail=detail,
            )

    def fresh(self, inputs: ResearchInputs) -> None:
        if self.now() >= inputs.expires_at:
            raise SyncError(RESEARCH_EXPIRED)

    def validate(self, name: str, arguments: dict[str, Any]) -> None:
        if (
            name not in ALLOWED
            or arguments.keys() - ALLOWED[name]
            or name not in self.schemas
            or not Draft202012Validator(self.schemas[name]).is_valid(arguments)
        ):
            raise SyncError(
                NON_DISPLAY_INPUT,
                stage="tool call",
            )

    async def _request(self, send):
        if isinstance(self.client, MeteredClient):
            return await send()
        return await self.account.request(send)

    @_account_guard
    async def call_raw(
        self, name: str, arguments: dict[str, Any], *, inputs=None
    ) -> tuple[Any, str]:
        """Transport and parse, plus the fixed shape class of what came back."""
        self.validate(name, arguments)
        async with self.lock:
            # No high-level tool-call negotiation/replay on an ambiguous mutation.
            try:

                async def send():
                    if inputs is not None:
                        self.fresh(inputs)
                    result = await self.client.session.call_tool(
                        self.wire[name], arguments, read_timeout_seconds=self.account.timeout()
                    )
                    if result.is_error:
                        raise SyncError(
                            CALL_FAILED,
                            stage="tool call",
                            detail=f"tool {self.wire[name]}; {_error_word(result)}",
                        )
                    return result

                result = await self._request(send)
            except SyncError:
                raise
            except MCPError as error:
                _sanitized(
                    SyncError(
                        SESSION_REFUSED,
                        stage="tool call",
                        detail=f"JSON-RPC error {error.code}",
                    )
                )
            except Exception:
                error = SyncError(
                    SESSION_REFUSED,
                    stage="tool call",
                )
                _sanitized(error)
        if result.is_error:
            raise SyncError(
                CALL_FAILED,
                stage="tool call",
                detail=f"tool {self.wire[name]}; {_error_word(result)}",
            )
        value = result.structured_content
        if value is not None:
            return value, _write_shape(value, "structured_content")
        texts = [part.text for part in result.content if part.type == "text"]
        if len(texts) != 1:
            return _UNPARSED, "content:multiple" if texts else "content:empty"
        try:
            value = json.loads(texts[0], parse_float=Decimal)
        except ValueError:
            return _UNPARSED, "content:unparsed"
        return value, _write_shape(value, "content")

    @_account_guard
    async def call(self, name: str, arguments: dict[str, Any], *, inputs=None) -> Any:
        value, shape = await self.call_raw(name, arguments, inputs=inputs)
        if value is _UNPARSED:
            raise SyncError(UNKNOWN_RESULT_ENVELOPE, stage="tool call", detail=shape)
        return _unwrap(value)


def _id(row: dict[str, Any]) -> str:
    values = [row[key] for key in ("id", "watchlist_id") if key in row]
    if (
        not values
        or any(
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or not str(value).isascii()
            or not str(value).isdigit()
            for value in values
        )
        or len({str(value) for value in values}) != 1
    ):
        raise SyncError(INVALID_LIST_ID, stage="tool call")
    return str(values[0])


async def _membership(tools: OfficialTools, name: str, key: str) -> set[str]:
    row = await tools.call("get_watchlist", {"watchlist_id": key})
    if isinstance(row, dict) and "watchlist" in row and set(row) <= {"watchlist", "note"}:
        row = _unwrap(row["watchlist"])
    if (
        not isinstance(row, dict)
        or row.get("name") != name
        or _id(row) != key
        or row.get("read_only") not in (None, False)
    ):
        raise SyncError(LIST_READ_ONLY, stage="tool call")
    symbols = row.get("symbols")
    if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
        raise SyncError(UNKNOWN_MEMBERSHIP_ENVELOPE, stage="tool call", detail=_shape(symbols))
    return set(symbols)


async def _inventory(tools: OfficialTools) -> dict[str, tuple[str, set[str]]]:
    rows = await tools.call("list_watchlists", {})
    rows = rows.get("watchlists") if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SyncError(UNKNOWN_INVENTORY_ENVELOPE, stage="tool call", detail=_shape(rows))
    result = {}
    for name in NAMES:
        matches = [row for row in rows if row.get("name") == name]
        if len(matches) > 1:
            raise SyncError(DUPLICATE_LIST_NAME, stage="tool call")
        if not matches:
            continue
        key = _id(matches[0])
        references = sum(
            any(
                isinstance(row.get(field), (str, int))
                and not isinstance(row.get(field), bool)
                and str(row[field]) == key
                for field in ("id", "watchlist_id")
            )
            for row in rows
        )
        if references != 1:
            raise SyncError(LIST_ALIAS, stage="tool call")
        result[name] = key, await _membership(tools, name, key)
    return result


async def read_memberships(tools: OfficialTools) -> dict[str, frozenset[str]]:
    """Complete by-ID read-back; an absent reserved list is the known-empty fresh install.

    The display loop seeds ``Cadence`` with this pair so the first admission compares
    against observed membership, never a guessed empty or cleared list. Unknown,
    duplicate or aliased inventory raises before any run is claimed.
    """
    await tools.discover()
    inventory = await _inventory(tools)
    return {
        name: frozenset(inventory[name][1]) if name in inventory else frozenset() for name in NAMES
    }


async def sync_research(
    tools: OfficialTools, inputs: ResearchInputs, *, apply=False
) -> dict[str, Any]:
    """Preflight all operations, apply once, then verify the two named lists."""
    if (
        set(inputs.memberships) != set(NAMES)
        or not inputs.memberships["NS movers"]
        or len(inputs.unmapped) > 5
    ):
        raise SyncError(
            INVALID_INPUTS,
            stage="inputs",
        )
    tools.fresh(inputs)
    await tools.discover()
    current = await _inventory(tools)
    operations = []
    for name in NAMES:
        desired = inputs.memberships[name]
        if name not in current:
            operations.append(
                {
                    "tool": "create_watchlist",
                    "arguments": {"name": name, "symbols": sorted(desired)},
                }
            )
            continue
        key, stored = current[name]
        for tool, delta in (
            ("add_to_watchlist", desired - stored),
            ("remove_from_watchlist", stored - desired),
        ):
            if delta:
                operations.append(
                    {"tool": tool, "arguments": {"watchlist_id": key, "symbols": sorted(delta)}}
                )
    for operation in operations:
        tools.validate(operation["tool"], operation["arguments"])
    tools.fresh(inputs)
    result = {
        "applied": False,
        "operations": operations,
        "readback": {},
        "unmapped": sorted(inputs.unmapped),
    }
    if not apply:
        return result
    for operation in operations:
        if operation["tool"] != "create_watchlist":
            response, shape = await tools.call_raw(
                operation["tool"], operation["arguments"], inputs=inputs
            )
            if not _documented_write(response):
                raise SyncError(
                    AMBIGUOUS_MUTATION,
                    stage="apply",
                    detail=shape,
                )
            continue
        response = await tools.call(operation["tool"], operation["arguments"], inputs=inputs)
        # Validate creation identity before any later mutation. Re-inventory
        # also detects duplicate names and aliases before final readback.
        row = response
        if isinstance(row, dict) and "watchlist" in row and set(row) <= {"watchlist", "note"}:
            row = _unwrap(row["watchlist"])
        if (
            not isinstance(row, dict)
            or row.get("name") != operation["arguments"]["name"]
            or row.get("read_only") not in (None, False)
            or not isinstance(row.get("symbols"), list)
            or any(not isinstance(symbol, str) for symbol in row["symbols"])
        ):
            raise SyncError(
                UNKNOWN_CREATE_RESULT,
                stage="apply",
            )
        if _id(row) in {item[0] for item in current.values()}:
            raise SyncError(
                CREATE_ALIAS,
                stage="apply",
            )
    if any(op["tool"] == "create_watchlist" for op in operations):
        current = await _inventory(tools)
    rejected_all = set()
    for name in NAMES:
        if name not in current:
            raise SyncError(CREATE_MISSING, stage="apply")
        key = current[name][0]
        stored = await _membership(tools, name, key)
        desired = inputs.memberships[name]
        rejected, extras = desired - stored, stored - desired
        if extras:
            response, shape = await tools.call_raw(
                "remove_from_watchlist",
                {"watchlist_id": key, "symbols": sorted(extras)},
                inputs=inputs,
            )
            if not _documented_write(response):
                raise SyncError(AMBIGUOUS_REMOVAL, stage="apply", detail=shape)
            stored = await _membership(tools, name, key)
        result["readback"][name] = {
            "stored": sorted(stored),
            "rejected": sorted(rejected),
            "corrective_removals": sorted(extras),
        }
        rejected_all.update(rejected)
        if stored != desired - rejected:
            raise SyncError(
                READBACK_MISMATCH,
                stage="apply",
            )
    if len(rejected_all) > 5:
        raise SyncError(REJECTED_TOO_MANY)
    result["applied"] = True
    return result
