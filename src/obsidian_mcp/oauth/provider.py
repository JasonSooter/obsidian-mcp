"""OAuth 2.1 authorization server for the MCP endpoint.

Implements the MCP SDK's OAuthAuthorizationServerProvider. The flow is the
"MCP server is itself the authorization server" shape -- there is no upstream
identity provider, because there is exactly one user. Instead, `authorize()`
parks the request and redirects to our own login page (see login.py), which
authenticates with password + TOTP and then completes the redirect.

    Claude ──/authorize──► this server ──redirect──► /login (password + TOTP)
                                                        │
    Claude ◄──?code=…──────────────────────────────────┘
      │
      └──/token──► access token (+ refresh)
"""

# Ported from anki-mcp's oauth package, which solved this first. The two are
# near-identical by design: same flow, same threat model, one user each. Fix
# bugs in both.

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from ..auth import OBSIDIAN_SCOPE
from .store import Store, hash_secret, new_secret

log = logging.getLogger(__name__)

# Short: the code is exchanged immediately by the client.
AUTH_CODE_TTL_SECONDS = 300
# The connector re-authorizes silently with the refresh token, so a short-lived
# access token costs nothing and limits the damage from one leaking.
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
# How long you have to finish the login form after Claude sends you there.
PENDING_LOGIN_TTL_SECONDS = 600

# Single user, so the subject is a constant. It exists to satisfy the token
# model and to make logs readable.
SUBJECT = "owner"


class ObsidianOAuthProvider:
    """Single-user authorization server backed by the SQLite Store."""

    def __init__(self, store: Store, login_path: str = "/login") -> None:
        self._store = store
        self._login_path = login_path

    # -- client registration ----------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Dynamic registration is open, which sounds alarming but is what the
        # MCP spec expects: registering only gets you the *right to ask* for
        # authorization. Nothing is issued until the password + TOTP login
        # succeeds, so an unauthenticated registration is inert.
        self._store.put_client(
            client_info.client_id, client_info.model_dump(mode="json")
        )
        log.info(
            "registered oauth client %s (%s)",
            client_info.client_id,
            client_info.client_name or "unnamed",
        )

    # -- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to our login page."""
        login_id = new_secret()
        self._store.put_pending(
            login_id,
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [OBSIDIAN_SCOPE],
                "resource": params.resource,
            },
            PENDING_LOGIN_TTL_SECONDS,
        )
        self._store.purge_expired()
        return f"{self._login_path}?login_id={login_id}"

    def complete_login(self, login_id: str) -> str | None:
        """Called by the login route once password + TOTP have been verified.

        Issues the authorization code and returns the full redirect URL back to
        the client. Returns None if the pending login expired.
        """
        pending = self._store.take_pending(login_id)
        if pending is None:
            return None

        code = new_secret()
        self._store.put_code(
            code,
            {
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_uri_provided_explicitly": pending[
                    "redirect_uri_provided_explicitly"
                ],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "resource": pending["resource"],
            },
            time.time() + AUTH_CODE_TTL_SECONDS,
        )
        self._store.drop_pending(login_id)

        # Built with urllib rather than string concatenation. `state` is
        # chosen by the client and interpolating it raw breaks the redirect on
        # any reserved character. The naive '?' test is also wrong for a
        # redirect_uri carrying a fragment, which would land the parameters
        # inside the fragment instead of the query.
        parts = urlsplit(pending["redirect_uri"])
        query = parse_qsl(parts.query, keep_blank_values=True)
        query.append(("code", code))
        if pending["state"]:
            query.append(("state", pending["state"]))
        url = urlunsplit(parts._replace(query=urlencode(query)))
        log.info("issued authorization code to client %s", pending["client_id"])
        return url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._store.get_code(authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data["resource"],
            subject=SUBJECT,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # PKCE verification is done by the SDK before this is called; our job is
        # to burn the code so it cannot be replayed.
        if self._store.get_code(authorization_code.code) is None:
            raise TokenError("invalid_grant", "Authorization code is no longer valid")
        self._store.consume_code(authorization_code.code)
        return self._issue_tokens(
            client.client_id, authorization_code.scopes, authorization_code.resource
        )

    # -- tokens ------------------------------------------------------------

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        access, refresh = new_secret(), new_secret()
        now = time.time()
        common: dict[str, Any] = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "subject": SUBJECT,
        }
        self._store.put_token(
            access, "access", common, now + ACCESS_TOKEN_TTL_SECONDS
        )
        self._store.put_token(
            refresh, "refresh", common, now + REFRESH_TOKEN_TTL_SECONDS
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._store.get_token(refresh_token, "refresh")
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=None,
            subject=SUBJECT,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = self._store.get_token(refresh_token.token, "refresh")
        if data is None:
            raise TokenError("invalid_grant", "Refresh token is no longer valid")
        # Rotate: the spec says SHOULD, and without rotation a stolen refresh
        # token is effectively permanent access.
        self._store.delete_token(refresh_token.token)
        return self._issue_tokens(
            client.client_id, scopes or data["scopes"], data.get("resource")
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._store.get_token(token, "access")
        if data is None:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            resource=data.get("resource"),
            subject=SUBJECT,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._store.delete_token(getattr(token, "token", ""))
