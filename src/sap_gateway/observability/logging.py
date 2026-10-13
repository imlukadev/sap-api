"""Logging estruturado em JSON (stdout) e ponte opcional para o pipeline de logs do OpenTelemetry.

Os dois destinos recebem o **mesmo** registro, já sanitizado e enriquecido pelos filtros:

* stdout: ``JsonFormatter`` (independe de qualquer backend);
* OTLP:   ``opentelemetry.sdk._logs.LoggingHandler`` (atributos nativos + trace_id/span_id).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
import weakref
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import IO

from opentelemetry import trace

from sap_gateway.context import current_request, current_soap
from sap_gateway.observability.sanitize import (
    MAX_STACKTRACE_LENGTH,
    is_sensitive_key,
    redact_text,
    sanitize_value,
)

_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName"}
)
_MAX_EXTRAS = 32
_SANITIZED: weakref.WeakSet[logging.LogRecord] = weakref.WeakSet()
_HANDLER_FLAG = "_sap_gateway_handler"

NOISY_LOGGERS = {
    "zeep": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpx2": logging.WARNING,
    "opentelemetry.exporter": logging.ERROR,
}
"""``zeep.transports`` registra envelopes completos em DEBUG — inclusive WS-Security."""


class ContextFilter(logging.Filter):
    """Adiciona, de forma automática, usuário, rota e operação SOAP ao registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        request = current_request()
        if request is not None:
            if request.username:
                record.__dict__.setdefault("username", request.username)
            if request.route:
                record.__dict__.setdefault("http.route", request.route)
            if request.method:
                record.__dict__.setdefault("http.request.method", request.method)
        soap = current_soap()
        if soap is not None:
            record.__dict__.setdefault("soap.service", soap.service)
            record.__dict__.setdefault("soap.operation", soap.operation)
        return True


class SanitizingFilter(logging.Filter):
    """Redige mensagem, atributos e exceções; idempotente entre handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record in _SANITIZED:
            return True
        record.msg = redact_text(record.getMessage())
        record.args = None

        extras = [key for key in record.__dict__ if key not in _STANDARD_ATTRS]
        for key in extras[_MAX_EXTRAS:]:
            del record.__dict__[key]
        for key in extras[:_MAX_EXTRAS]:
            record.__dict__[key] = sanitize_value(key, record.__dict__[key])

        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc, tb = record.exc_info
            record.__dict__["exception.type"] = exc_type.__name__
            record.__dict__["exception.message"] = redact_text(str(exc))
            record.__dict__["exception.stacktrace"] = redact_text(
                "".join(traceback.format_exception(exc_type, exc, tb)),
                limit=MAX_STACKTRACE_LENGTH,
            )
        record.exc_info = None
        record.exc_text = None
        _SANITIZED.add(record)
        return True


class ExcludeLoggersFilter(logging.Filter):
    """Evita laço de realimentação: falhas do exporter OTLP não voltam para o próprio exporter."""

    def __init__(self, prefixes: Iterable[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(self._prefixes)


class JsonFormatter(logging.Formatter):
    _CONTEXT_KEYS = (
        "username",
        "http.route",
        "http.request.method",
        "http.response.status_code",
        "duration_ms",
        "soap.service",
        "soap.operation",
        "soap.attempts",
        "soap.outcome",
        "error.type",
        "exception.type",
        "exception.message",
        "exception.stacktrace",
    )

    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._static = {"service.name": service_name, "deployment.environment": environment}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            **self._static,
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = f"{span_context.trace_id:032x}"
            payload["span_id"] = f"{span_context.span_id:016x}"
        for key in self._CONTEXT_KEYS:
            if key in record.__dict__ and record.__dict__[key] is not None:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, ensure_ascii=False, default=str)


class InstalledLogging:
    """Handle para remover exatamente o que foi instalado (usado no shutdown e nos testes)."""

    def __init__(self, handlers: list[logging.Handler]) -> None:
        self.handlers = handlers

    def uninstall(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()
        self.handlers.clear()


def _remove_previous_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
            handler.close()


def install_logging(
    *,
    level: str,
    service_name: str,
    environment: str,
    otel_handler: logging.Handler | None = None,
    stream: IO[str] | None = None,
) -> InstalledLogging:
    """Configura o root logger. Idempotente: remove handlers instalados anteriormente."""
    root = logging.getLogger()
    _remove_previous_handlers(root)

    shared_filters = (ContextFilter(), SanitizingFilter())
    console = logging.StreamHandler(stream or sys.stdout)
    console.setFormatter(JsonFormatter(service_name=service_name, environment=environment))
    handlers: list[logging.Handler] = [console]

    if otel_handler is not None:
        otel_handler.addFilter(ExcludeLoggersFilter(("opentelemetry", "grpc")))
        handlers.append(otel_handler)

    for handler in handlers:
        for flt in shared_filters:
            handler.addFilter(flt)
        setattr(handler, _HANDLER_FLAG, True)
        root.addHandler(handler)

    root.setLevel(level)
    for name, noisy_level in NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(noisy_level, logging.getLevelNamesMapping()[level]))
    logging.getLogger("uvicorn.access").disabled = True
    return InstalledLogging(handlers)


__all__ = [
    "ContextFilter",
    "ExcludeLoggersFilter",
    "InstalledLogging",
    "JsonFormatter",
    "SanitizingFilter",
    "install_logging",
    "is_sensitive_key",
]
