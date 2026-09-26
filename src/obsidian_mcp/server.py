"""Server assembly: which tools exist, and who is allowed to call them."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import OBSIDIAN_SCOPE
from .config import Config
from .oauth.login import register_routes as register_login_routes
from .oauth.provider import ObsidianOAuthProvider
from .oauth.store import Store
from .oauth.totp import normalise_secret
from .tools import history, publish, read, write
from .tools._common import Context

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read and write an Obsidian vault stored as markdown files on disk.

Paths are always relative to the vault root, e.g. 'Projects/Media server.md'.
Use search_query, search_simple or vault_list to discover exact paths before
reading or writing --
do not guess them.

The vault is also synced by Dropbox, so it can change between calls. Every tool
reads fresh from disk. When an edit depends on what a note already says, pass
the mtime_ns from vault_read into vault_write's expected_mtime_ns so a concurrent
change fails the edit instead of being silently overwritten.

vault_append adds to the end of a note or a section; vault_patch is the precise
version, targeting a heading, a ^block-id, or a frontmatter property.

Every append, patch, write, move and delete saves the previous contents first;
list_versions and restore_version undo them. vault_delete also moves the note to
.trash/ rather than destroying it.
"""

PUBLISH_INSTRUCTIONS = """\

Notes flagged `publish: true` are published to a public website. set_publish_status
changes the flag; publish_site is what actually pushes, and it defaults to
dry_run=True. Publishing is the one action here that cannot be undone -- always
review a dry run before calling it with dry_run=False.
"""


def build(config: Config, ctx: Context) -> tuple[MCPServer, Starlette]:
    """Construct the MCP server and its Streamable HTTP app.

    Auth is OAuth 2.1 throughout: the SDK is handed a full authorization-server
    provider and derives its own token verifier. Tool code never sees a token.
    """
    instructions = INSTRUCTIONS
    if ctx.publisher is not None:
        instructions += PUBLISH_INSTRUCTIONS

    store = Store(config.oauth_db_path)
    oauth_provider = ObsidianOAuthProvider(store)
    auth_settings = AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=config.public_url,
            required_scopes=[OBSIDIAN_SCOPE],
            # Claude registers itself when you add the connector; without
            # dynamic registration you would have to pre-provision a client id
            # and paste it into the connector dialog.
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[OBSIDIAN_SCOPE],
                default_scopes=[OBSIDIAN_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

    mcp = MCPServer(
        name="obsidian",
        title="Obsidian Vault",
        version="0.1.0",
        instructions=instructions,
        auth_server_provider=oauth_provider,
        # Enabling auth here is what makes the SDK reject unauthenticated
        # requests before they ever reach a tool.
        auth=auth_settings,
    )

    assert config.login_password and config.totp_secret
    register_login_routes(
            mcp,
            oauth_provider,
            store,
            config.login_password,
            normalise_secret(config.totp_secret),
        )

    read.register(mcp, ctx)
    write.register(mcp, ctx)
    history.register(mcp, ctx)

    # Never-registered-never-reachable: with QUARTZ_ENABLED=false the publish
    # tools are not constructed at all, so a direct tools/call by name gets the
    # SDK's unknown-tool error rather than a permission check we might get wrong.
    if ctx.publisher is not None:
        publish.register(mcp, ctx)
        log.info("quartz publishing ENABLED (repo=%s)", config.quartz.repo_path)
    else:
        log.info("quartz publishing disabled; publish tools not registered")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        """Liveness probe. Unauthenticated, and deliberately contentless.

        This endpoint is reachable from the public internet via Funnel, so it
        reports only whether the vaults are readable -- never their names, note
        counts, or anything else about what is inside them.
        """
        healthy = True
        for vault in ctx.vaults.all():
            if not vault.root.is_dir():
                log.error("vault root unreachable: %s", vault.root)
                healthy = False
        body: dict[str, Any] = {
            "status": "ok" if healthy else "vault unreachable",
            "vaults_reachable": healthy,
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    app = mcp.streamable_http_app(host=config.host)
    return mcp, app
