"""Entrypoint.

Order matters: config is validated, then the vaults and state directory are
checked, and only then does the server start listening. A failure in either of
the first two steps exits non-zero -- this server never comes up half-working,
because a container that answers health checks while unable to reach the vault
is worse than one that visibly restarts.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from . import telemetry
from .config import ConfigError, load
from .oauth.totp import InvalidTOTPSecret
from .journal import Journal
from .quartz import Publisher
from .server import build
from .tools._common import Context
from .vault import build_registry

log = logging.getLogger("obsidian_mcp")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    try:
        config = load()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    try:
        vaults = build_registry(
            config.vault_root, config.vault_names, config.default_vault
        )
    except ValueError as exc:
        log.error("vault error: %s", exc)
        log.error(
            "check that your sync tool (Dropbox, rclone, Syncthing...) has finished its initial sync and "
            "that OBSIDIAN_VAULT_ROOT points at the directory holding the vaults"
        )
        return 3

    # The journal is what makes the mutating tools safe to expose. If we cannot
    # write snapshots, we must not come up -- serving vault_write and vault_delete
    # with no undo is exactly the situation this design exists to avoid.
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.versions_dir.mkdir(parents=True, exist_ok=True)
        probe = config.state_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        log.error(
            "state directory %s is not writable (%s). Refusing to start: "
            "without it, mutations would be unrecoverable.",
            config.state_dir,
            exc,
        )
        return 4

    journal = Journal(
        config.versions_dir,
        config.audit_log_path,
        retention_days=config.journal_retention_days,
    )
    journal.prune()

    publisher = None
    if config.quartz is not None:
        quartz = config.quartz
        if not quartz.repo_path.is_dir():
            log.error(
                "QUARTZ_ENABLED is true but %s is not a directory. Clone the "
                "Quartz repo there, or set QUARTZ_ENABLED=false.",
                quartz.repo_path,
            )
            return 5
        if quartz.ssh_key_path and not quartz.ssh_key_path.exists():
            log.error(
                "QUARTZ_SSH_KEY_PATH %s does not exist. Mount the deploy key, "
                "or set QUARTZ_ENABLED=false.",
                quartz.ssh_key_path,
            )
            return 5
        publisher = Publisher(quartz, config.state_dir)

    ctx = Context(
        config=config, vaults=vaults, journal=journal, publisher=publisher
    )
    # Telemetry after config (it needs the environment name) but before the
    # server starts, so startup problems are shipped too. A missing OTLP
    # endpoint disables it silently -- the vault must stay servable whether or
    # not Grafana is reachable.
    telemetry.setup(
        service_name="obsidian-mcp",
        environment=config.environment,
        version="0.1.0",
    )
    telemetry.emit(
        telemetry.EVENT_STARTUP,
        vaults=",".join(config.vault_names),
        quartz_enabled=publisher is not None,
        port=config.port,
    )
    _emit_vault_stats(ctx)

    # build() inside the try: it can raise on a malformed config -- a TOTP
    # secret that is not valid base32 is the concrete case -- and an operator
    # deserves an exit code and a message rather than a stack trace. Keeping it
    # here also guarantees telemetry is flushed on that path.
    try:
        try:
            _, app = build(config, ctx)
        except (ConfigError, InvalidTOTPSecret) as exc:
            log.error("configuration error: %s", exc)
            return 2

        log.info(
            "serving MCP on http://%s:%s/mcp (vaults: %s, auth: oauth)",
            config.host,
            config.port,
            ", ".join(config.vault_names),
        )
        uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    finally:
        telemetry.shutdown()
    return 0


def _emit_vault_stats(ctx: Context) -> None:
    """One gauge-style event per vault at startup.

    Backs the dashboard's 'how big is the vault' panels. Emitted once rather
    than on a timer: counting notes walks the whole tree, and this server's
    whole design is that it does not hold a picture of the vault between calls.
    """
    for vault in ctx.vaults.all():
        try:
            notes = sum(1 for _ in vault.iter_notes())
        except OSError:
            continue
        telemetry.emit(telemetry.EVENT_VAULT_STATS, vault=vault.name, notes=notes)


if __name__ == "__main__":
    sys.exit(main())
