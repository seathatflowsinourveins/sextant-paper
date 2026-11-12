"""RAM-only SDK OAuth by default, adapted from northstar-trading@8256c16c auth.py.

The owner's D-23 ``--keep-login`` store is the one opt-in that keeps the official
SDK's tokens and registration encrypted at rest (``stored_login.py``).

MCP SDK v2.3.0 docs_src/oauth_clients/tutorial001.py supplies the memory store
and provider. Upstream's quiet loopback callback avoids pasted codes or URLs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import webbrowser
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client as SDKClient
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from sextant.tradingview import diagnostics
from sextant.tradingview.account import Account, GuardedHTTPTransport, HTTPGate
from sextant.tradingview.account import MeteredClient as Client
from sextant.tradingview.stored_login import (
    RefreshGuard,
    StoredLogin,
    StoredLoginCorrupt,
    StoredLoginError,
    StoredTokenStorage,
    _is_refresh,
)
from sextant.tradingview.sync import (
    LOGIN_NEEDS_CONSENT,
    LOGIN_REFUSED,
    LOGIN_STOPS,
    LOGIN_STORE_UNAVAILABLE,
    LOGIN_UNAVAILABLE,
    LOGIN_UNREADABLE,
    LOGIN_UNUSABLE,
    SyncError,
    _sanitized,
)

SERVER = "https://mcp.tradingview.com/mcp"
LOGIN_STOP_BY_REASON = {
    "refused": LOGIN_REFUSED,
    "unavailable": LOGIN_UNAVAILABLE,
    "unusable": LOGIN_UNUSABLE,
    "consent": LOGIN_NEEDS_CONSENT,
}
OWNER_STEP = (
    "TradingView owner browser consent required each session: run --authorize on the owner's "
    "machine with Essential+ (non-trial). Authorization stays in RAM unless --keep-login keeps "
    "it in the encrypted store; never paste credentials or callback URLs into a terminal or chat."
)


_logging_lock = Lock()
_logging_users = 0
_previous_logging = logging.NOTSET


@contextmanager
def sensitive_logging() -> Iterator[None]:
    """northstar-trading@8256c16c auth_safety: last user restores logging.

    No await occurs under the lock. Changing logging policy during OAuth is
    unsupported; overlapping users share the original application's policy.
    """
    global _logging_users, _previous_logging
    with _logging_lock:
        if _logging_users == 0:
            _previous_logging = logging.root.manager.disable
            logging.disable(max(_previous_logging, logging.CRITICAL))
        _logging_users += 1
    try:
        yield
    finally:
        with _logging_lock:
            _logging_users -= 1
            if _logging_users == 0:
                logging.disable(_previous_logging)


class InMemoryTokenStorage:
    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class OwnerCallback:
    def __init__(self) -> None:
        self.result: AuthorizationCodeResult | None = None
        self.failed = False
        callback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                valid = (
                    all(len(query.get(key, [])) == 1 for key in ("code", "state"))
                    and len(query.get("iss", [])) <= 1
                    and "error" not in query
                )
                if valid:
                    callback.result = AuthorizationCodeResult(
                        code=query["code"][0],
                        state=query["state"][0],
                        iss=query.get("iss", [None])[0],
                    )
                else:
                    callback.failed = True
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Return to the Sextant terminal. Do not copy this URL.")

            def log_message(self, format: str, *args: Any) -> None:
                # Default HTTP request logs contain the OAuth callback code/state.
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 1
        self.uri = f"http://127.0.0.1:{self.server.server_port}/callback"

    async def redirect(self, authorization_url: str) -> None:
        if urlsplit(authorization_url).scheme != "https" or not webbrowser.open(authorization_url):
            raise SyncError(OWNER_STEP, stage="authorize")

    async def wait(self) -> AuthorizationCodeResult:
        def receive():
            deadline = time.monotonic() + 300
            while self.result is None and not self.failed and time.monotonic() < deadline:
                self.server.handle_request()
            if self.result is None:
                raise SyncError(OWNER_STEP, stage="callback")
            return self.result

        return await asyncio.to_thread(receive)


async def identify_request(request: httpx2.Request) -> None:
    # Upstream measured missing SDK discovery headers rejected by CloudFront.
    request.headers.setdefault("User-Agent", "sextant-tradingview-sync/1 (display-only)")
    request.headers.setdefault("Accept", "application/json")


def _stored_storage(login: StoredLogin) -> tuple[StoredTokenStorage, bool]:
    """Read the store before any session, so a bad value stops with its own owner step."""
    storage = StoredTokenStorage(login)
    try:
        return storage, storage.preload()
    except StoredLoginCorrupt:
        _sanitized(SyncError(LOGIN_UNREADABLE, stage="authorize"))
    except (OSError, StoredLoginError):
        _sanitized(SyncError(LOGIN_STORE_UNAVAILABLE, stage="authorize"))


def _login_stop(guard: RefreshGuard | None) -> SyncError | None:
    reason = guard.reason() if guard is not None else ""
    return SyncError(LOGIN_STOP_BY_REASON[reason], stage="token") if reason else None


@asynccontextmanager
async def official_client(
    authorize: bool = False, login: StoredLogin | None = None
) -> AsyncIterator[SDKClient]:
    """RAM-only unless the owner's ``--keep-login`` store is passed; no other grant reuse.

    Only ``authorize`` opens a browser. With the store and no ``authorize`` the stored
    sign-in is used or the run stops with the owner step (an unattended unit's mode).
    """
    storage: InMemoryTokenStorage | StoredTokenStorage = InMemoryTokenStorage()
    guard = None
    stored = False
    if login is not None:
        # A first --keep-login must write the consent into the store, not RAM.
        storage, stored = _stored_storage(login)
        guard = RefreshGuard(storage, consent=authorize)
    if not authorize and not stored:
        raise SyncError(OWNER_STEP if login is None else LOGIN_NEEDS_CONSENT, stage="authorize")
    Account().preflight()
    callback = None
    gate = HTTPGate(
        lambda r: (
            r.status_code == 401
            or (r.status_code == 404 and r.request.url.path.startswith("/.well-known/"))
            or (guard is not None and _is_refresh(r.request) and not guard.reason())
        )
    )
    with sensitive_logging():
        try:
            callback = OwnerCallback()
            oauth = OAuthClientProvider(
                server_url=SERVER,
                client_metadata=OAuthClientMetadata(
                    client_name="Sextant display-only sync", redirect_uris=[AnyUrl(callback.uri)]
                ),
                storage=storage,
                redirect_handler=(
                    callback.redirect if authorize or guard is None else guard.redirect
                ),
                callback_handler=callback.wait,
            )
            hooks: dict[str, list[Any]] = {"request": [identify_request]}
            if isinstance(storage, StoredTokenStorage) and guard is not None:
                # The pinned SDK's `_initialize` loads the document without setting
                # `token_expiry_time` (oauth2.py:565-569), so it would treat an old access
                # token as valid and answer a 401 with browser consent instead of the
                # stored refresh token. Prime the same field `update_token_expiry` sets.
                storage.prime_expiry(oauth.context)
                guard.context = oauth.context
                hooks = {"request": [identify_request, guard.request], "response": [guard.response]}
            hooks["request"].append(gate.request)
            hooks.setdefault("response", []).append(gate.response)
            async with httpx2.AsyncClient(
                transport=GuardedHTTPTransport(httpx2.AsyncHTTPTransport(), gate),
                auth=oauth,
                timeout=httpx2.Timeout(30.0, read=60.0),
                event_hooks=hooks,
            ) as http_client:
                if guard is not None and stored:
                    # Any later refresh needs the published token endpoint, not a fallback.
                    await guard.discover(http_client)
                transport = streamable_http_client(SERVER, http_client=http_client)
                body_error = None
                async with Client(
                    transport,
                    read_timeout_seconds=Account().timeout(),
                    mode="legacy",
                    http_gate=gate,
                ) as client:
                    try:
                        yield client
                    except SyncError as error:
                        # SDK task groups wrap errors leaving Client; close normally
                        # before propagating a trusted local refusal to the caller.
                        body_error = error
            # A stored-login stop outranks what the body made of it, including a body
            # (the loop) that caught the failure and returned normally.
            stop = _login_stop(guard)
            if stop is not None:
                _sanitized(stop)
            if body_error is not None:
                raise body_error
        except asyncio.CancelledError:
            _sanitized(asyncio.CancelledError())
        except SyncError as error:
            stop = _login_stop(guard)
            if stop is not None and str(error) not in LOGIN_STOPS:
                _sanitized(stop)
            raise
        except Exception as error:
            stop = _login_stop(guard)
            if stop is not None:
                _sanitized(stop)
            _sanitized(
                SyncError(
                    "TradingView session stopped; re-inventory before retry. " + OWNER_STEP,
                    label=diagnostics.leaf_label(error),
                    stage=diagnostics.stage(error),
                    detail=diagnostics.detail(error),
                )
            )
        finally:
            gate.finish(False)
            if isinstance(storage, InMemoryTokenStorage):
                storage.tokens = None
                storage.client_info = None
            if callback is not None:
                callback.server.server_close()
                callback.result = None
