"""Environment-driven configuration.

Secrets (the login password and TOTP secret) come from the environment only --
never from the repo's committed .env, which by its own comment holds non-secret
ids and the timezone.

There is deliberately no Profile enum here. An earlier design split the server
into 'full' and 'capture' profiles on separate ports; that was collapsed to a
single service so one connector URL works on every device, including the phone
and claude.ai, which can only reach a public address. What replaces the split
as a safety mechanism is journal.py: every mutation is reversible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# The login password is the outer wall on a public endpoint. TOTP is the second
# factor, but a weak password makes the pair only as strong as six digits.
MIN_PASSWORD_LENGTH = 12

# Refuse to read anything pathological into a model's context.
DEFAULT_MAX_FILE_BYTES = 2_000_000


class ConfigError(RuntimeError):
    """Raised at startup for a config problem that must stop the process."""


@dataclass(frozen=True)
class QuartzConfig:
    """Everything needed to build and push the Quartz site."""

    repo_path: Path
    remote: str
    branch: str
    ssh_key_path: Path | None
    git_author: str
    # False when the repo builds via a CI workflow on push: publish_site then
    # skips the local `npx quartz build` and only commits and pushes.
    build_locally: bool


@dataclass(frozen=True)
class Config:
    vault_root: Path
    vault_names: tuple[str, ...]
    default_vault: str
    state_dir: Path
    login_password: str | None
    totp_secret: str | None
    host: str
    port: int
    public_url: str
    max_file_bytes: int
    journal_retention_days: int
    environment: str
    quartz: QuartzConfig | None

    @property
    def versions_dir(self) -> Path:
        """Where pre-mutation snapshots live. Outside the vault on purpose:
        Dropbox must never sync them, and they must never appear in search."""
        return self.state_dir / "versions"

    @property
    def oauth_db_path(self) -> Path:
        """Clients, codes, and tokens. Survives restarts so a connector added
        on a phone does not have to be re-authorized after every deploy."""
        return self.state_dir / "oauth.db"

    @property
    def audit_log_path(self) -> Path:
        return self.state_dir / "audit.log"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if value else None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None


def load() -> Config:
    """Read config from the environment, or raise ConfigError.

    Deliberately strict: it is better to refuse to boot than to come up
    unauthenticated or pointed at the wrong directory.
    """
    vault_root = Path(_env("OBSIDIAN_VAULT_ROOT", "/vaults") or "/vaults")
    # Required, with no default: a guessed vault name would point the server
    # at a directory that does not exist, or at the wrong one.
    raw_vaults = _env("OBSIDIAN_VAULTS") or ""
    vault_names = tuple(n.strip() for n in raw_vaults.split(",") if n.strip())
    if not vault_names:
        raise ConfigError(
            "OBSIDIAN_VAULTS must be set: a comma-separated list of vault folder "
            f"names under OBSIDIAN_VAULT_ROOT ({vault_root}), e.g. OBSIDIAN_VAULTS=MyVault."
        )

    default_vault = _env("OBSIDIAN_DEFAULT_VAULT") or vault_names[0]
    if default_vault not in vault_names:
        raise ConfigError(
            f"OBSIDIAN_DEFAULT_VAULT={default_vault!r} is not in OBSIDIAN_VAULTS "
            f"({', '.join(vault_names)})."
        )

    login_password = _env("OBSIDIAN_MCP_LOGIN_PASSWORD")
    totp_secret = _env("OBSIDIAN_MCP_TOTP_SECRET")

    # Both factors are mandatory. The login page is the only thing between the
    # public internet and the vault, so a half-configured deploy must fail at
    # boot rather than come up serving with weaker auth than intended.
    missing = [
        name
        for name, value in (
            ("OBSIDIAN_MCP_LOGIN_PASSWORD", login_password),
            ("OBSIDIAN_MCP_TOTP_SECRET", totp_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"{', '.join(missing)} must be set. Authentication is OAuth with a "
            "password + TOTP login; the server will not start without both "
            "factors configured."
        )
    if login_password and len(login_password) < MIN_PASSWORD_LENGTH:
        raise ConfigError(
            f"OBSIDIAN_MCP_LOGIN_PASSWORD is only {len(login_password)} "
            f"characters; at least {MIN_PASSWORD_LENGTH} are required."
        )

    # 8780, not 8770, so it can run beside anki-mcp (which uses 8770). Under
    # some Docker runtimes (Colima, for one) an occupied host port fails
    # silently rather than erroring -- the container looks healthy while every
    # request reaches the other service -- so a distinct default matters.
    port = _env_int("OBSIDIAN_MCP_PORT", 8780)

    quartz: QuartzConfig | None = None
    # Off unless asked for: publishing needs a Quartz repo checked out at
    # QUARTZ_REPO_PATH, and most installs have none.
    if _env_bool("QUARTZ_ENABLED", False):
        key_raw = _env("QUARTZ_SSH_KEY_PATH")
        quartz = QuartzConfig(
            repo_path=Path(_env("QUARTZ_REPO_PATH", "/quartz") or "/quartz"),
            remote=_env("QUARTZ_GIT_REMOTE", "origin") or "origin",
            branch=_env("QUARTZ_GIT_BRANCH", "main") or "main",
            ssh_key_path=Path(key_raw) if key_raw else None,
            git_author=_env("QUARTZ_GIT_AUTHOR", "obsidian-mcp <obsidian-mcp@localhost>")
            or "obsidian-mcp <obsidian-mcp@localhost>",
            build_locally=_env_bool("QUARTZ_BUILD_LOCALLY", True),
        )

    return Config(
        vault_root=vault_root,
        vault_names=vault_names,
        default_vault=default_vault,
        state_dir=Path(_env("OBSIDIAN_MCP_STATE_DIR", "/state") or "/state"),
        login_password=login_password,
        totp_secret=totp_secret,
        host=_env("OBSIDIAN_MCP_HOST", "0.0.0.0") or "0.0.0.0",
        port=port,
        # Only feeds OAuth discovery metadata, so a localhost default is
        # harmless -- but set it to the Funnel hostname in production so the
        # metadata a client fetches actually names the URL it reached.
        public_url=_env("OBSIDIAN_MCP_PUBLIC_URL") or f"http://localhost:{port}",
        max_file_bytes=_env_int("OBSIDIAN_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
        journal_retention_days=_env_int("OBSIDIAN_JOURNAL_RETENTION_DAYS", 30),
        # Becomes the deployment_environment_name label in Loki; the bundled
        # Grafana dashboard filters on it.
        environment=_env("OBSIDIAN_MCP_ENV", "production") or "production",
        quartz=quartz,
    )
