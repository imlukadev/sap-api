"""Erros do consumo SOAP. Mensagens públicas são fixas: nunca carregam conteúdo vindo do SAP."""

from __future__ import annotations

from sap_gateway.errors import AppError


class SoapUnavailableError(AppError):
    """WSDL/XSD não pôde ser carregado, ou endpoint inseguro fora de local/test."""

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


class SoapHttpError(AppError):
    """Resposta HTTP de erro que não é um SOAP Fault (4xx, 5xx, redirect)."""

    status_code = 502
    code = "soap_transport_error"
    public_message = "O serviço de destino respondeu com erro HTTP."
    error_type = "http_status"

    def __init__(self, *, upstream_status: int) -> None:
        super().__init__()
        self.upstream_status = upstream_status


class SoapFaultError(AppError):
    """SOAP Fault. ``detail``: conteúdo de ``<detail>`` como dict (nome local → texto/dict)."""

    status_code = 502
    code = "soap_fault"
    public_message = "O serviço de destino retornou um erro (SOAP Fault)."
    error_type = "soap_fault"

    def __init__(
        self,
        *,
        fault_code: str | None,
        fault_message: str | None,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.fault_code = fault_code
        self.fault_message = fault_message
        self.detail = detail or {}

    def find(self, name: str) -> str | None:
        """Valor do primeiro elemento ``name`` em ``detail`` (busca em profundidade)."""
        return _find(self.detail, name)


class SoapResponseError(AppError):
    """XML malformado, elemento inesperado ou resposta fora do modelo esperado."""

    status_code = 502
    code = "soap_response_error"
    public_message = "O serviço de destino retornou uma resposta inválida."
    error_type = "invalid_response"


class SoapRequestError(AppError):
    """A requisição não pôde ser serializada conforme o WSDL (defeito de modelo/alias)."""

    status_code = 500
    code = "soap_request_error"
    public_message = "Não foi possível montar a requisição para o serviço de destino."
    error_type = "serialization"


def _find(node: dict[str, object], name: str) -> str | None:
    for key, value in node.items():
        if key == name and isinstance(value, str):
            return value
        if isinstance(value, dict) and (found := _find(value, name)) is not None:
            return found
    return None
