"""OTLP telemetry, for any OTLP backend (Grafana Cloud is what the bundled
dashboard targets).

Logs go out over OTLP, land in Loki, and every structured field is queryable
as structured metadata --

    {service_name="obsidian-mcp", deployment_environment_name="production"}
      | tool="vault_append" | status="error"

Configuration is the standard OTel environment: OTEL_EXPORTER_OTLP_ENDPOINT and
OTEL_EXPORTER_OTLP_HEADERS. With no endpoint set, telemetry is a no-op and the
server logs to stdout as usual -- the server must not fail to serve the vault
because an observability backend is unreachable.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger(__name__)

_enabled = False
_provider: Any = None

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

# Emitted as the log body; the dashboard counts these by name.
EVENT_TOOL_CALL = "tool call"
EVENT_LOGIN = "oauth login"
EVENT_MUTATION = "vault mutation"
EVENT_VAULT_STATS = "vault stats"
EVENT_STARTUP = "server started"


def setup(*, service_name: str, environment: str, version: str) -> bool:
    """Wire OTLP log export, if an endpoint is configured. Returns enabled."""
    global _enabled, _provider

    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; telemetry disabled")
        return False

    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
    except ImportError:
        log.warning("opentelemetry packages missing; telemetry disabled")
        return False

    try:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": version,
                # The dashboard's $env template variable filters on this.
                "deployment.environment.name": environment,
            }
        )
        _provider = LoggerProvider(resource=resource)
        _provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

        # Attaching to the root logger means every existing log.info/exception in
        # this codebase ships too, without threading a telemetry object through
        # modules that have no other reason to know about it.
        handler = LoggingHandler(level=logging.INFO, logger_provider=_provider)
        logging.getLogger().addHandler(handler)
        _enabled = True
        log.info("telemetry enabled: service=%s env=%s", service_name, environment)
        return True
    except Exception:
        # Never let an observability problem stop the server from serving.
        log.exception("could not initialise telemetry; continuing without it")
        return False


def shutdown() -> None:
    """Flush pending log records. Best effort."""
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            log.exception("telemetry shutdown failed")


def emit(event: str, *, level: int = logging.INFO, **attributes: Any) -> None:
    """Emit one structured event.

    Attributes become Loki structured metadata, so keep the keys stable -- the
    dashboard queries them by name. Values are scalars only; a dict or list
    would be flattened to a string and become useless to query.
    """
    # Booleans are stringified: Loki structured-metadata filters compare
    # strings, so `| mutation="true"` works uniformly while a real bool would
    # depend on how the backend renders it.
    clean = {
        _safe_key(k): (str(v).lower() if isinstance(v, bool) else v)
        for k, v in attributes.items()
        if v is not None
    }
    try:
        logging.getLogger("obsidian_mcp.telemetry").log(level, event, extra=clean)
    except Exception:
        # Observability must never take down the vault. journal.record() passes
        # arbitrary per-tool fields through here, so a bad key must degrade to a
        # missing dashboard panel, not a failed tool call.
        log.warning("could not emit telemetry event %r", event, exc_info=True)


# Names already used by logging.LogRecord. Passing one of these in `extra=`
# raises KeyError("Attempt to overwrite ..."), which -- because journal.record()
# forwards tool fields verbatim -- would surface as the *tool* failing. Found
# the hard way: `created` on vault_append.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def _safe_key(key: str) -> str:
    """Rename an attribute that would collide with a LogRecord field."""
    if key in _RESERVED:
        return f"{key}_attr"
    return key


def instrumented(name: str, *, mutation: bool = False) -> Callable[[F], F]:
    """Time a tool call and emit its outcome.

    Applied *under* @mcp.tool so the SDK still sees the real signature through
    functools.wraps -- the tool's JSON schema is generated from the wrapped
    function's annotations, and losing them would silently break the schema.
    """

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            status = "ok"
            error_kind = None
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                status = "error"
                error_kind = type(exc).__name__
                raise
            finally:
                emit(
                    EVENT_TOOL_CALL,
                    level=logging.ERROR if status == "error" else logging.INFO,
                    tool=name,
                    status=status,
                    error=error_kind,
                    mutation=mutation,
                    # `vault` is a keyword arg on every tool; recording it keeps
                    # the panels correct if a second vault is added later.
                    vault=kwargs.get("vault"),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )

        return wrapper  # type: ignore[return-value]

    return decorate



def record_login(*, success: bool, client_ip: str, reason: str | None = None) -> None:
    """An attempt at the OAuth login page.

    Failures here are the loudest signal the server has: once Funnel is on, the
    login page is the only thing guarding the whole vault, so someone probing it
    matters. The submitted password and code are never recorded.
    """
    emit(
        EVENT_LOGIN,
        level=logging.INFO if success else logging.WARNING,
        status="ok" if success else "failed",
        client_ip=client_ip,
        reason=reason,
    )
