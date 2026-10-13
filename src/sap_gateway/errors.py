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


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    public_message = "A operação conflita com o estado atual do recurso."
    error_type = "conflict"


class BusinessRuleError(AppError):
    status_code = 422
    code = "business_rule_violation"
    public_message = "A operação foi rejeitada por uma regra de negócio do sistema de destino."
    error_type = "business_rule"


class SoapWsdlUnavailableError(AppError):
    status_code = 503
    code = "soap_contract_unavailable"
    public_message = "O contrato (WSDL/XSD) do serviço de destino está indisponível."
    error_type = "wsdl_unavailable"


class SoapConnectionError(AppError):
    status_code = 502
    code = "soap_connection_error"
    public_message = "Não foi possível conectar ao serviço de destino."
    error_type = "connection"

    def __init__(self, *, request_sent: bool, transient: bool = True) -> None:
        super().__init__()
        self.request_sent = request_sent
        self.transient = transient


class SoapTimeoutError(AppError):
    status_code = 504
    code = "soap_timeout"
    public_message = "O serviço de destino não respondeu dentro do tempo limite."
    error_type = "timeout"

    def __init__(self, *, request_sent: bool) -> None:
        super().__init__()
        self.request_sent = request_sent


class SoapTransportError(AppError):
    status_code = 502
    code = "soap_transport_error"
    public_message = "O serviço de destino respondeu com erro HTTP."
    error_type = "http_status"

    def __init__(self, *, upstream_status: int) -> None:
        super().__init__()
        self.upstream_status = upstream_status


class SoapFaultError(AppError):
    """SOAP Fault. ``detail`` é o modelo de fault declarado no contrato da operação (se houver)."""

    status_code = 502
    code = "soap_fault"
    public_message = "O serviço de destino retornou um erro (SOAP Fault)."
    error_type = "soap_fault"

    def __init__(
        self,
        *,
        fault_code: str | None,
        fault_message: str | None,
        detail: BaseModel | None = None,
    ) -> None:
        super().__init__()
        self.fault_code = fault_code
        self.fault_message = fault_message
        self.detail = detail


class SoapMalformedResponseError(AppError):
    status_code = 502
    code = "soap_malformed_response"
    public_message = "O serviço de destino retornou uma resposta inválida."
    error_type = "malformed_response"


class ContractMappingError(AppError):
    """Dados que não respeitam o contrato (resposta SOAP fora do contrato)."""

    status_code = 502
    code = "contract_mismatch"
    public_message = "A resposta do serviço de destino não respeita o contrato esperado."
    error_type = "contract_mismatch"


class SoapSerializationError(AppError):
    """A requisição montada não pôde ser serializada conforme o XSD (defeito de mapeamento)."""

    status_code = 500
    code = "soap_serialization_error"
    public_message = "Não foi possível montar a requisição para o serviço de destino."
    error_type = "serialization"


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
