"""Middleware ASGI puro: abre o contexto da requisição e emite o log de acesso estruturado.

Fica *dentro* do middleware do OpenTelemetry, portanto o span do servidor já está ativo e o log
carrega ``trace_id``/``span_id`` nativamente. Não lê nem registra cabeçalhos ``Authorization``.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sap_gateway.context import RequestContext, request_scope

logger = logging.getLogger("sap_gateway.access")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, quiet_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self._quiet_paths = quiet_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ctx = RequestContext(method=scope["method"], scope=scope)
        status_code: int | None = None
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        with request_scope(ctx):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                status_code = status_code or 500
                raise
            finally:
                if scope["path"] not in self._quiet_paths:
                    self._log(scope, ctx, status_code or 500, time.perf_counter() - started)

    @staticmethod
    def _log(scope: Scope, ctx: RequestContext, status: int, elapsed: float) -> None:
        extra: dict[str, object] = {
            "http.response.status_code": status,
            "duration_ms": round(elapsed * 1000, 1),
        }
        error_type = scope.get("state", {}).get("error_type")
        if error_type:
            extra["error.type"] = error_type
        level = logging.INFO if status < 400 else logging.WARNING if status < 500 else logging.ERROR
        logger.log(level, "http request completed", extra=extra)
