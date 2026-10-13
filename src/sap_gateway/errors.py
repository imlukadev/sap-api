"""Hierarquia de erros internos tipados e sua conversão para respostas HTTP estruturadas.

Regra: a mensagem pública de cada erro é fixa e nunca contém conteúdo vindo do SOAP
(faults, corpos de resposta, URLs internas) nem credenciais. O detalhe técnico fica em
logs/traces, de forma sanitizada.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    field: str | None = Field(default=None, description="Localização do campo inválido.")
    message: str


class ErrorBody(BaseModel):
    code: str = Field(description="Código estável e legível por máquina.")
    message: str = Field(description="Descrição segura para o consumidor.")
    trace_id: str | None = Field(
        default=None, description="Identificador do trace, para suporte e correlação."
    )
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base de todos os erros esperados da aplicação."""

    status_code: int = 500
    code: str = "internal_error"
    public_message: str = "Erro interno inesperado."
    error_type: str = "internal"

    def __init__(
        self,
        *,
        public_message: str | None = None,
        details: list[ErrorDetail] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(self.code)
        if public_message is not None:
            self.public_message = public_message
        self.details = details
        self.headers = dict(headers) if headers else None


class AuthenticationRequiredError(AppError):
    status_code = 401
    code = "authentication_required"
    public_message = "Credenciais HTTP Basic ausentes ou inválidas."
    error_type = "authentication"

    def __init__(self, *, realm: str = "sap-gateway") -> None:
        super().__init__(headers={"WWW-Authenticate": f'Basic realm="{realm}"'})


class AuthenticationFailedError(AppError):
    """O serviço SOAP rejeitou as credenciais (HTTP 401 / fault de autenticação)."""

    status_code = 401
    code = "authentication_failed"
    public_message = "Usuário ou senha rejeitados pelo sistema de destino."
    error_type = "authentication"

    def __init__(self, *, realm: str = "sap-gateway") -> None:
        super().__init__(headers={"WWW-Authenticate": f'Basic realm="{realm}"'})


class AuthorizationError(AppError):
    status_code = 403
    code = "forbidden"
    public_message = "O usuário não tem autorização para esta operação."
    error_type = "authorization"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    public_message = "Recurso não encontrado."
    error_type = "not_found"


class BusinessRuleError(AppError):
    status_code = 422
    code = "business_rule_violation"
    public_message = "A operação foi rejeitada por uma regra de negócio do sistema de destino."
    error_type = "business_rule"


def _trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: list[ErrorDetail] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, trace_id=_trace_id(), details=details)
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=dict(headers) if headers else None,
    )


_HTTP_STATUS_CODES = {
    401: ("authentication_required", "Credenciais HTTP Basic ausentes ou inválidas."),
    403: ("forbidden", "Acesso negado."),
    404: ("route_not_found", "Rota não encontrada."),
    405: ("method_not_allowed", "Método HTTP não permitido para esta rota."),
}


def install_error_handlers(app: FastAPI, *, basic_realm: str = "sap-gateway") -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        request.scope.setdefault("state", {})["error_type"] = exc.error_type
        trace.get_current_span().set_attribute("error.type", exc.error_type)
        return error_response(
            exc.status_code, exc.code, exc.public_message, details=exc.details, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            ErrorDetail(
                field=".".join(str(part) for part in err["loc"]),
                message=str(err["msg"]),
            )
            for err in exc.errors()
        ]
        return error_response(
            422, "validation_error", "Dados de entrada inválidos.", details=details
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_STATUS_CODES.get(exc.status_code, ("http_error", "Erro HTTP."))
        headers = None
        if exc.status_code == 401:
            headers = {"WWW-Authenticate": f'Basic realm="{basic_realm}"'}
        return error_response(exc.status_code, code, message, headers=headers)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled exception", exc_info=exc, extra={"error.type": type(exc).__name__})
        return error_response(500, "internal_error", AppError.public_message)
