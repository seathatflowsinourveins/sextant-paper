"""The owner's lasting TradingView sign-in in the repo's encrypted systemd-creds store.

D-23 (docs/adr/0021 amendment) authorises one stored sign-in for the display-only
sync: one JSON line of the official SDK's tokens and client registration, written
and read only through ``scripts/keys.sh`` and ``systemd-creds --with-key=host``.
No plain file, environment variable, argv value or log line carries it.

The pinned SDK's ``TokenStorage`` protocol is the seam (mcp 2.3.0,
``mcp/client/auth/oauth2.py``). ``StoredTokenStorage`` is used only when
``--keep-login`` passes a ``StoredLogin``; the in-memory store stays the default.
``RefreshGuard`` decides what a failed refresh means for the stored value.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never
from urllib.parse import parse_qs

import httpx2
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    issuers_match,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

NAME = "TRADINGVIEW_MCP_OAUTH"
SEALED = "NS_SEALED_TRANSFER_COMPLETE"
ROOT = Path(__file__).resolve().parents[3]
KEYS = ROOT / "scripts" / "keys.sh"
SYSTEMD = ROOT / "scripts" / "key-systemd.sh"


class StoredLoginError(RuntimeError):
    """The store refused the sign-in; the message is a fixed literal, never its value."""


class StoredLoginCorrupt(StoredLoginError):
    """The stored value cannot be decrypted or parsed; only --forget-login clears it."""


class StoredLoginStop(RuntimeError):
    """The refresh guard stopped the session; ``RefreshGuard.stop`` names why."""


@dataclass(frozen=True)
class StoredDocument:
    tokens: OAuthToken
    client_info: OAuthClientInformationFull | None
    obtained_at: float


def _run(
    command: list[str], home: Path | None, **kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(command, capture_output=True, check=False, env=env, **kwargs)


def _sanitized(error: BaseException) -> Never:
    """Upstream auth_safety.raise_sanitized; no retained raw exception graph."""
    try:
        raise error from None
    except BaseException:
        error.__context__ = None
        error.__cause__ = None
        raise


class StoredLogin:
    """One encrypted JSON line under ``$HOME/.config/sextant/credentials``."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home

    def _state(self) -> str:
        for script in (KEYS, SYSTEMD):
            if not script.is_file():
                raise StoredLoginError("the repo's key store script is missing")
        result = _run(
            [
                "bash",
                "-c",
                'source "$1"; ns_systemd_init; ns_systemd_state "$2"',
                "keys",
                str(SYSTEMD),
                NAME,
            ],
            self._home,
        )
        state = result.stdout.decode(errors="replace").strip()
        if result.returncode == 44 or state == "missing":
            return "missing"
        if result.returncode != 0 or state != "set":
            raise StoredLoginError("the sign-in store is unavailable")
        return "set"

    def present(self) -> bool:
        return self._state() == "set"

    def load(self) -> StoredDocument | None:
        if self._state() == "missing":
            return None
        result = _run(
            [
                "bash",
                "-c",
                'source "$1"; ns_systemd_init; ns_systemd_read "$2"',
                "keys",
                str(SYSTEMD),
                NAME,
            ],
            self._home,
        )
        if result.returncode != 0:
            raise StoredLoginCorrupt("the stored sign-in could not be read")
        try:
            payload = json.loads(result.stdout)
            tokens = OAuthToken.model_validate(payload["tokens"])
            client_info = (
                OAuthClientInformationFull.model_validate(payload["client_info"])
                if payload.get("client_info")
                else None
            )
            obtained_at = float(payload["obtained_at"])
        except (ValueError, KeyError, TypeError):
            _sanitized(StoredLoginCorrupt("the stored sign-in is unreadable"))
        return StoredDocument(tokens=tokens, client_info=client_info, obtained_at=obtained_at)

    def save(
        self,
        tokens: OAuthToken,
        client_info: OAuthClientInformationFull | None,
        obtained_at: float | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "tokens": tokens.model_dump(mode="json"),
                "client_info": client_info.model_dump(mode="json")
                if client_info is not None
                else None,
                "obtained_at": time.time() if obtained_at is None else obtained_at,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        if not payload.isprintable():
            raise StoredLoginError("the sign-in does not fit the store's one printable line")
        result = _run(
            ["bash", str(KEYS), "set", NAME, "--sealed-stdin"],
            self._home,
            input=f"{payload}\n{SEALED}\n".encode(),
        )
        if result.returncode != 0:
            raise StoredLoginError("the store refused the sign-in")

    def forget(self) -> bool:
        if self._state() == "missing":
            return False
        result = _run(["bash", str(KEYS), "rm", NAME], self._home)
        if result.returncode != 0:
            raise StoredLoginError("the store refused to remove the sign-in")
        return True


class StoredTokenStorage:
    """The SDK's TokenStorage protocol over one ``StoredLogin``; writes merge the document."""

    def __init__(self, login: StoredLogin) -> None:
        self._login = login
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None
        self._obtained_at = 0.0
        self._loaded = False
        self._lock = asyncio.Lock()

    def _load(self) -> None:
        if self._loaded:
            return
        document = self._login.load()
        if document is not None:
            self._tokens = document.tokens
            self._client_info = document.client_info
            self._obtained_at = document.obtained_at
        self._loaded = True

    def _persist(self) -> None:
        if self._tokens is not None:
            self._login.save(self._tokens, self._client_info, self._obtained_at)

    def preload(self) -> bool:
        """Read the store once before any session; True when it holds a token."""
        self._load()
        return self._tokens is not None

    def forget(self, *, drop_client: bool) -> None:
        """Delete the refused value and the RAM copy the SDK would reload (oauth2.py:618-620)."""
        self._login.forget()
        self._tokens = None
        self._obtained_at = 0.0
        if drop_client:
            self._client_info = None

    def prime_expiry(self, context: Any) -> None:
        """Set the SDK's expiry field so an old stored token refreshes, not re-consents."""
        self._load()
        if self._tokens is not None and self._tokens.expires_in:
            context.token_expiry_time = self._obtained_at + int(self._tokens.expires_in)

    async def get_tokens(self) -> OAuthToken | None:
        self._load()
        return self._tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        self._load()
        return self._client_info

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._lock:
            self._load()
            self._tokens = tokens
            self._obtained_at = time.time()
            # The registration is written with the first token so a fresh consent
            # keeps the client id the next refresh needs.
            self._persist()

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            self._load()
            self._client_info = client_info
            if self._tokens is not None:
                self._persist()


# RFC 6749 section 5.2 codes by which the authorization server says this client's stored
# grant will not be honoured. The pinned SDK's own server answers a dead refresh token with
# 400 invalid_grant, a failed client authentication with 401 invalid_client, and a client
# not permitted the grant with unauthorized_client (mcp/server/auth/handlers/token.py:69-74,
# 89-92, 105-113, 204-219, 255-262; mcp/server/auth/provider.py:106-115).
REFUSED_CODES = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


def _is_refresh(request: httpx2.Request) -> bool:
    """The SDK's refresh request: a form POST with grant_type=refresh_token (oauth2.py:517-531)."""
    if request.method != "POST":
        return False
    try:
        form = parse_qs(request.content.decode("ascii"))
    except (httpx2.RequestNotRead, UnicodeDecodeError):
        return False
    return form.get("grant_type") == ["refresh_token"]


async def _refusal_code(response: httpx2.Response) -> str:
    """One of REFUSED_CODES from a section 5.2 error body, else empty; the body is not kept."""
    if response.status_code not in (400, 401):
        return ""
    try:
        document = json.loads(await response.aread())
    except ValueError:
        return ""
    code = document.get("error") if isinstance(document, dict) else None
    return code if isinstance(code, str) and code in REFUSED_CODES else ""


class RefreshGuard:
    """Classifies the token endpoint's answer to a refresh before the SDK discards it.

    The pinned SDK clears its tokens on any non-200 refresh (mcp/client/auth/oauth2.py:533-538)
    and then answers the next 401 with a browser authorization (:613-631, :784-786), so an
    outage would read as a refusal. httpx2 runs request and response event hooks for every
    request an auth flow yields (httpx2/_client.py:1877-1883), which is where this reads it:

    - 5xx or 429, or a transport error while the refresh is in flight: stop, store unchanged.
    - 400 or 401 carrying one of REFUSED_CODES: delete the stored value, then either continue
      to the SDK's one browser sign-in (``consent``) or stop with the owner step.
    - anything else: stop, store unchanged.

    Without ``consent`` a browser sign-in the SDK asks for also stops (``redirect``).
    """

    def __init__(self, storage: StoredTokenStorage, *, consent: bool) -> None:
        self._storage = storage
        self._consent = consent
        self._in_flight = False
        self.context: Any = None
        self.stop = ""

    def _halt(self, reason: str) -> Never:
        self.stop = reason
        raise StoredLoginStop(reason)

    def reason(self) -> str:
        """Why the session stopped, if this guard stopped it or a refresh never answered."""
        return self.stop or ("unavailable" if self._in_flight else "")

    async def discover(self, http: httpx2.AsyncClient) -> None:
        """Know the token endpoint before a stored login's first refresh, or stop "unavailable".

        The pinned SDK discovers the authorization server only after a 401 (oauth2.py:631-718),
        and `_initialize` loads only tokens and registration (:564-568), so a fresh process
        refreshes at the fallback `<server>/token` (:511-515), which the live service refuses.
        This runs the SDK's own discovery steps first, from what the store already holds: the
        issuer the SDK bound to the registration (SEP-2352, :766-781), else the server's
        protected-resource well-known (:645-667). The requests skip the OAuth flow (no token).
        """
        context = self.context
        client = await self._storage.get_client_info()
        issuer = client.issuer if client is not None else None
        try:
            if issuer is None:
                for url in build_protected_resource_metadata_discovery_urls(
                    None, context.server_url
                ):
                    prm = await handle_protected_resource_response(await _get(http, url))
                    if prm is not None:
                        wanted = resource_url_from_server_url(context.server_url)
                        if prm.resource is None or not check_resource_allowed(
                            requested_resource=wanted, configured_resource=str(prm.resource)
                        ):
                            self._halt("unavailable")
                        context.protected_resource_metadata = prm
                        issuer = str(prm.authorization_servers[0])
                        break
                else:
                    self._halt("unavailable")
            for url in build_oauth_authorization_server_metadata_discovery_urls(
                issuer, context.server_url
            ):
                found, metadata = await handle_auth_metadata_response(await _get(http, url))
                if not found:
                    break
                if metadata is not None and issuers_match(str(metadata.issuer), issuer):
                    context.oauth_metadata = metadata
                    context.auth_server_url = str(metadata.issuer)
                    return
        except (httpx2.HTTPError, ValueError):
            self._halt("unavailable")
        self._halt("unavailable")

    async def request(self, request: httpx2.Request) -> None:
        if _is_refresh(request):
            # A refresh is never sent to a fallback endpoint (oauth2.py:511-515).
            metadata = getattr(self.context, "oauth_metadata", None)
            if metadata is None or request.url != httpx2.URL(str(metadata.token_endpoint)):
                self._halt("unavailable")
            self._in_flight = True

    async def response(self, response: httpx2.Response) -> None:
        if not _is_refresh(response.request):
            return
        self._in_flight = False
        status = response.status_code
        if status == 200:
            return
        if status >= 500 or status == 429:
            self._halt("unavailable")
        code = await _refusal_code(response)
        if not code:
            self._halt("unusable")
        try:
            self._storage.forget(drop_client=code == "invalid_client")
        except (OSError, StoredLoginError):
            self._halt("unusable")
        if code == "invalid_client" and self.context is not None:
            # The SDK drops credentials it cannot use so the flow re-registers (oauth2.py:684-694).
            self.context.client_info = None
        if not self._consent:
            self._halt("refused")

    async def redirect(self, authorization_url: str) -> None:
        """The SDK's redirect handler when the owner is absent: never opens a browser."""
        self._halt("consent")


async def _get(http: httpx2.AsyncClient, url: str) -> httpx2.Response:
    """The SDK's metadata request (utils.py:278-279), sent without the OAuth flow."""
    return await http.send(create_oauth_metadata_request(url), auth=None)
