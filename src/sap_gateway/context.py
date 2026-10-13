"""Contexto da requisição corrente, propagado via ``contextvars``.

Guarda apenas metadados de diagnóstico (usuário, método, rota). **Nunca** guarda senha ou
cabeçalho ``Authorization``: as credenciais trafegam por argumentos explícitos até o gateway SOAP.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RequestContext:
    method: str | None = None
    username: str | None = None
    scope: Mapping[str, Any] | None = field(default=None, repr=False)

    @property
    def route(self) -> str | None:
        """Template da rota (ex.: ``/api/v1/notifications/{notification_id}``)."""
        route = (self.scope or {}).get("route")
        return getattr(route, "path", None)


@dataclass(frozen=True, slots=True)
class SoapContext:
    service: str
    operation: str


_request_ctx: ContextVar[RequestContext | None] = ContextVar("request_ctx", default=None)
_soap_ctx: ContextVar[SoapContext | None] = ContextVar("soap_ctx", default=None)


def current_request() -> RequestContext | None:
    return _request_ctx.get()


def current_soap() -> SoapContext | None:
    return _soap_ctx.get()


@contextmanager
def request_scope(ctx: RequestContext) -> Iterator[RequestContext]:
    token = _request_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _request_ctx.reset(token)


@contextmanager
def soap_scope(service: str, operation: str) -> Iterator[None]:
    token = _soap_ctx.set(SoapContext(service, operation))
    try:
        yield
    finally:
        _soap_ctx.reset(token)


def set_username(username: str) -> None:
    ctx = _request_ctx.get()
    if ctx is not None:
        ctx.username = username
