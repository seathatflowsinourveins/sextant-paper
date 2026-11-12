"""LEAD-0903 account guard at the httpx2 2.16.0 request-hook boundary.

Source: shared/dispatcher.py Outbound; client/client.py _build_session;
client/session.py send_request/send_discover. Atomic writer/flock reuse Sextant.
HTTP hooks charge all methods, including setup, OAuth reads, stream GET and DELETE.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import sys
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.server import Server

from sextant.execution.state import write_atomic

DEFAULT_PARAMETERS = Path(__file__).resolve().parents[3] / "lanes/tradingview/account.params.json"
_ACTIVE: ContextVar[Any] = ContextVar("tv_account_transaction", default=None)
_SHARED: dict[Path, dict] = {}  # State whose flock this process already holds.
_sleep = asyncio.sleep


def _utcnow():
    return datetime.now(UTC)


class AccountError(ValueError):
    """Fixed local diagnostic; no upstream data."""


def _row(path, name):
    try:
        raw = json.loads(path.read_text())
        rows = [r for r in raw["rows"] if r["name"] == name]
        (row,) = rows
        if (
            raw["schema"] != "tv-account.registry/2"
            or not row["source"].strip()
            or date.fromisoformat(row["last_change"]).isoformat() != row["last_change"]
            or row["calibration_state"]
            != ("FIXED" if name == "account_state_path" else "UNCALIBRATED")
            or row["unit"]
            != {
                "account_state_path": "path",
                "account_calls_per_utc_day": "calls/UTC day",
                "account_request_min_interval_s": "seconds/request",
                "account_request_timeout_s": "seconds",
            }[name]
            or row["value"] is None
        ):
            raise ValueError
        return row["value"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AccountError("Account request authority unavailable") from None


def canonical_path():
    value = _row(DEFAULT_PARAMETERS, "account_state_path")
    if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
        raise AccountError("Canonical account state unavailable")
    return Path(value).expanduser().resolve()


def _page():
    print(
        "TradingView account stopped; lead review required before further calls.", file=sys.stderr
    )


class Account:
    def __init__(self, *, parameters=None, now=None):
        if parameters is not None and Path(parameters).resolve() != DEFAULT_PARAMETERS.resolve():
            raise AccountError("Only the canonical account registry is allowed")
        self.parameters = DEFAULT_PARAMETERS
        self.path = canonical_path()
        self.now = now or _utcnow

    def at(self):
        at = self.now()
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise AccountError("Explicit aware clock required")
        return at.astimezone(UTC)

    def policy(self):
        limit = _row(self.parameters, "account_calls_per_utc_day")
        spacing = _row(self.parameters, "account_request_min_interval_s")
        if type(limit) is not int or limit <= 0:
            raise AccountError("Daily call budget unavailable")
        # Both 0.75s and 1.5s failed in the incident; neither is a safe starting rate.
        if type(spacing) not in (int, float) or not math.isfinite(spacing) or spacing <= 1.5:
            raise AccountError("Account request spacing unavailable")
        return limit, spacing

    def timeout(self):
        timeout = _row(self.parameters, "account_request_timeout_s")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise AccountError("Account request timeout unavailable")
        return timeout

    def roll(self, state):
        day = self.at().date().isoformat()
        if day < state["day"]:
            raise AccountError("Regressing daily clock")
        if day > state["day"]:
            state.update(day=day, calls=0, stopped=False)
        # quota_stop and last_call never roll with the date.

    def load(self):
        try:
            state = json.loads(self.path.read_text())
            if (
                set(state)
                != {
                    "schema",
                    "day",
                    "calls",
                    "stopped",
                    "quota_stop",
                    "last_call",
                    "cache",
                    "audit",
                }
                or state["schema"] != "tv-account.state/2"
                or date.fromisoformat(state["day"]).isoformat() != state["day"]
                or type(state["calls"]) is not int
                or state["calls"] < 0
                or type(state["stopped"]) is not bool
                or type(state["quota_stop"]) is not bool
                or not isinstance(state["cache"], dict)
                or not isinstance(state["audit"], list)
            ):
                raise ValueError
            if state["last_call"] is not None:
                at = datetime.fromisoformat(state["last_call"])
                if at.utcoffset() is None:
                    raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise AccountError("Invalid or missing account state; no reset") from None
        self.roll(state)
        return state

    @contextmanager
    def locked(self):
        active = _ACTIVE.get()
        if active is not None and active[0] == self.path:
            yield active[1]
            return
        try:
            lock = self.path.with_name(self.path.name + ".lock").open("a")
        except OSError:
            raise AccountError("Account state unavailable; no reset") from None
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AccountError("Account state already has a writer") from None
            state = self.load()
            token = _ACTIVE.set((self.path, state))
            _SHARED[self.path] = state
            try:
                yield state
            finally:
                _SHARED.pop(self.path, None)
                _ACTIVE.reset(token)

    def snapshot(self):
        with self.locked() as state:
            return state.copy()

    def check(self, state):
        self.roll(state)
        if state["quota_stop"]:
            raise AccountError("Account quota stop latched; explicit lead clear required")
        if state["stopped"]:
            raise AccountError("Account day stopped; lead review required")
        limit, spacing = self.policy()
        self.timeout()
        if state["calls"] >= limit:
            raise AccountError("Daily call budget exhausted")
        return spacing

    def preflight(self):
        with self.locked() as state:
            self.check(state)

    def stop_day(self):
        with self.locked() as state:
            self.roll(state)
            state["stopped"] = True
            write_atomic(self.path, state)
        _page()

    async def reserve(self, state):
        spacing = self.check(state)
        if state["last_call"] is not None:
            delay = (
                spacing - (self.at() - datetime.fromisoformat(state["last_call"])).total_seconds()
            )
            if delay > 0:
                await _sleep(delay)
        self.check(state)  # Re-read authority and date after waiting.
        state.update(
            calls=state["calls"] + 1,
            stopped=True,
            quota_stop=True,
            last_call=self.at().isoformat(),
        )
        write_atomic(self.path, state)

    async def request(self, send: Callable[[], Awaitable[Any]]):
        with self.locked() as state:
            await self.reserve(state)
            try:
                result = await send()
            except BaseException:
                self.roll(state)
                state["stopped"] = True
                write_atomic(self.path, state)
                _page()
                raise
            self.roll(state)
            state.update(stopped=False, quota_stop=False)
            write_atomic(self.path, state)
            return result

    def record(self, action, lead, authority):
        if (
            not isinstance(lead, str)
            or not lead.strip()
            or not isinstance(authority, str)
            or not authority.strip()
        ):
            raise AccountError("Named lead and reviewed authority required")
        return {"action": action, "lead": lead, "authority": authority, "at": self.at().isoformat()}

    def initialize(self, *, lead, authority):
        record = self.record("init", lead, authority)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AccountError("Account state already has a writer") from None
            if self.path.exists():
                raise AccountError("Account state exists; no reset")
            write_atomic(
                self.path,
                {
                    "schema": "tv-account.state/2",
                    "day": self.at().date().isoformat(),
                    "calls": 0,
                    "stopped": True,
                    "quota_stop": True,
                    "last_call": None,
                    "cache": {},
                    "audit": [record],
                },
            )

    def clear(self, *, lead, authority):
        record = self.record("clear", lead, authority)
        with self.locked() as state:
            state["audit"].append(record)
            state.update(stopped=False, quota_stop=False)
            write_atomic(self.path, state)


def _refused(value):
    """MCP isError plus error envelopes; unknown quota text can never permit a probe."""
    if isinstance(value, dict):
        if value.get("isError") or value.get("error") or value.get("success") is False:
            return True
        text = value.get("text")
        if isinstance(text, str) and any(word in text.casefold() for word in ("quota", "suspend")):
            return True
        # mcp-types 2.3.0 Tool: these fields describe inputs/outputs, not answers.
        tool = isinstance(value.get("name"), str) and isinstance(value.get("inputSchema"), dict)
        return any(
            _refused(item)
            for key, item in value.items()
            if not (tool and key in {"inputSchema", "outputSchema"})
        )
    return isinstance(value, list) and any(_refused(item) for item in value)


class HTTPGate:
    """httpx2 2.16.0 _client.py:1877-1883: guard every physical HTTP request."""

    def __init__(self, known_response=lambda response: False):
        self.known_response = known_response
        self.account = Account()
        self.lock = asyncio.Lock()
        self.pending = None

    def release(self):
        if self.pending is not None:
            _, _, lease, _ = self.pending
            self.pending = None
            if lease is not None:
                lease()
            self.lock.release()

    async def request(self, request):
        await self.lock.acquire()
        lease = None
        try:
            state = _SHARED.get(self.account.path)
            if state is None:
                context = copy_context()
                scope = self.account.locked()
                state = context.run(scope.__enter__)

                def close_scope():
                    context.run(scope.__exit__, None, None, None)

                lease = close_scope

            try:
                body = json.loads(request.content) if request.method == "POST" else {}
            except (ValueError, httpx2.RequestNotRead):
                body = {}
            method = body.get("method") if isinstance(body, dict) and "id" in body else None
            self.pending = (request, state, lease, method)
            await self.account.reserve(state)
        except BaseException:
            if self.pending is None:
                if lease is not None:
                    lease()
                self.lock.release()
            else:
                self.release()
            raise

    def finish(self, accepted, *, request=None, method=None):
        if self.pending is None:
            return
        pending_request, state, _, pending_method = self.pending
        if request is not None and request is not pending_request:
            return
        if method is not None and method != pending_method:
            return
        try:
            self.account.roll(state)
            state.update(stopped=not accepted, quota_stop=not accepted)
            write_atomic(self.account.path, state)
            if not accepted:
                _page()
        finally:
            self.release()

    async def response(self, response):
        if self.pending is None or self.pending[0] is not response.request:
            return
        # SDK v2.3.0 streamable_http.py:224-273,704-705: optional endpoints.
        if response.status_code == 405 and response.request.method in {"GET", "DELETE"}:
            self.finish(True, request=response.request)
            return
        known = self.known_response(response)
        if (
            self.pending[3] is not None
            and response.status_code < 400
            and not known
            and not response.has_redirect_location
        ):
            return
        try:
            accepted = known or response.status_code < 400
            if known:
                await response.aread()
                accepted = not _refused({"text": response.text})
            elif "application/json" in response.headers.get("content-type", ""):
                await response.aread()
                accepted = accepted and not _refused(response.json())
            self.finish(accepted, request=response.request)
            if not accepted:
                raise AccountError("HTTP response refused; account stop latched")
        except BaseException:
            self.finish(False, request=response.request)
            raise


class GuardedHTTPTransport(httpx2.AsyncBaseTransport):
    def __init__(self, upstream, gate):
        self.upstream, self.gate = upstream, gate

    async def handle_async_request(self, request):
        try:
            return await self.upstream.handle_async_request(request)
        except BaseException:
            self.gate.finish(False, request=request)
            raise

    async def aclose(self):
        await self.upstream.aclose()


class MeteredDispatcher:
    def __init__(self, upstream, account, http_gate=None):
        self.upstream, self.account, self.http_gate = upstream, account, http_gate

    def __getattr__(self, name):
        return getattr(self.upstream, name)

    async def run(self, *args, **kwargs):
        return await self.upstream.run(*args, **kwargs)

    async def send_raw_request(self, method, params, opts=None):
        async def send():
            result = await self.upstream.send_raw_request(
                method, params, {**(opts or {}), "cancel_on_abandon": False}
            )
            if _refused(result):
                raise AccountError("Official request refused; account stop latched")
            return result

        if self.http_gate is None:
            return await self.account.request(send)
        try:
            result = await send()
        except BaseException:
            self.http_gate.finish(False, method=method)
            raise
        self.http_gate.finish(True, method=method)
        return result

    async def notify(self, method, params, opts=None):
        if self.http_gate is not None:
            return await self.upstream.notify(method, params, opts)
        return await self.account.request(lambda: self.upstream.notify(method, params, opts))


class MeteredClient(Client):
    """Pinned SDK seam: install guard before Client.__aenter__ starts negotiation."""

    def __init__(self, server, *, http_gate=None, **kwargs):
        self.http_gate = http_gate
        super().__init__(server, **kwargs)

    async def _build_session(self, exit_stack):
        if self.http_gate is None and not isinstance(self.server, Server):
            raise AccountError("External transport requires the account HTTP hooks")
        account = Account()
        account.preflight()
        session = await super()._build_session(exit_stack)
        session._dispatcher = MeteredDispatcher(
            session._dispatcher,
            account,
            None if isinstance(self.server, Server) else self.http_gate,
        )
        return session


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lead-only offline account stop control")
    parser.add_argument("action", choices=["init", "clear"])
    parser.add_argument("--lead", required=True)
    parser.add_argument("--authority", required=True)
    args = parser.parse_args(argv)
    try:
        account = Account()
        action = account.initialize if args.action == "init" else account.clear
        action(lead=args.lead, authority=args.authority)
    except AccountError:
        print("Account control refused; state unchanged.")
        return 2
    print("Lead action recorded; call authority is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
