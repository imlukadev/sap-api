"""Gateway SOAP reutilizável sobre o Zeep.

Isolamento de credenciais — por chamada (na verdade, por *tentativa*) criamos:

    requests.Session  →  RequestTransport  →  zeep.Client (+ wsse opcional)

Nada disso é reaproveitado entre requisições, e a sessão é fechada em ``finally``. As credenciais
chegam por argumento, nunca por estado global/``contextvars``. O único objeto compartilhado é o
``Document`` WSDL parseado (somente leitura, sem credenciais de usuário).
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests
from lxml import etree
from opentelemetry.trace import SpanKind, Status, StatusCode
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError
from zeep import Client, Plugin
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import Fault, TransportError
from zeep.wsdl import Document
from zeep.wsse.username import UsernameToken

from sap_gateway.auth import BasicCredentials
from sap_gateway.config import Environment, Settings, SoapAuthMode, SoapServiceSettings
from sap_gateway.context import soap_scope
from sap_gateway.errors import (
    AppError,
    AuthenticationFailedError,
    AuthorizationError,
    ContractMappingError,
    SoapConnectionError,
    SoapFaultError,
    SoapMalformedResponseError,
    SoapSerializationError,
    SoapTimeoutError,
    SoapTransportError,
    SoapWsdlUnavailableError,
)
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.soap.contracts import SoapModel, SoapOperation, build_request_args
from sap_gateway.soap.plain import to_plain
from sap_gateway.soap.retry import backoff_delay, is_retryable
from sap_gateway.soap.transport import (
    LocationNotAllowedError,
    RequestTransport,
    UpstreamHttpError,
    build_request_session,
)
from sap_gateway.soap.wsdl import WsdlStore, hardened_zeep_settings

logger = logging.getLogger(__name__)

_SOAP_ENVELOPE_NAMESPACES = (
    "http://schemas.xmlsoap.org/soap/envelope/",
    "http://www.w3.org/2003/05/soap-envelope",
)
_SOAP_BODY_TAGS = tuple(f"{{{ns}}}Body" for ns in _SOAP_ENVELOPE_NAMESPACES)
_SOAP_FAULT_PATHS = tuple(f"{{{ns}}}Body/{{{ns}}}Fault" for ns in _SOAP_ENVELOPE_NAMESPACES)
_INSECURE_ALLOWED: frozenset[Environment] = frozenset({"local", "test"})


class _ResponseCapture(Plugin):
    """Guarda o envelope da resposta *desta* tentativa (instância própria por Client)."""

    def __init__(self) -> None:
        self.envelope: etree._Element | None = None

    def ingress(self, envelope: etree._Element, http_headers: Any, operation: Any) -> Any:
        self.envelope = envelope
        return envelope, http_headers

    def has_soap_fault(self) -> bool:
        """O Zeep levanta ``Fault`` também para erro HTTP sem ``<Fault>``; aqui distinguimos."""
        if self.envelope is None:
            return False
        return any(self.envelope.find(path) is not None for path in _SOAP_FAULT_PATHS)

    def body_child(self) -> etree._Element | None:
        if self.envelope is None:
            return None
        for tag in _SOAP_BODY_TAGS:
            body = self.envelope.find(tag)
            if body is not None and len(body):
                return body[0]
        return None


def _connect_phase_failure(exc: requests.exceptions.ConnectionError) -> bool:
    """True se a falha foi anterior ao envio (recusa, DNS, timeout de conexão)."""
    for arg in exc.args:
        reason = arg.reason if isinstance(arg, MaxRetryError) else arg
        if isinstance(reason, NewConnectionError | ConnectTimeoutError):
            return True
    return False


class SoapGateway:
    def __init__(
        self,
        settings: Settings,
        telemetry: Telemetry,
        *,
        wsdl_store: WsdlStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._telemetry = telemetry
        self._wsdl = wsdl_store or WsdlStore()
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter de backoff, não criptografia

    @property
    def wsdl_store(self) -> WsdlStore:
        return self._wsdl

    def call[Req: SoapModel, Resp: SoapModel](
        self, operation: SoapOperation[Req, Resp], request: Req, credentials: BasicCredentials
    ) -> Resp:
        service_key, op_name = operation.service.key, operation.name
        cfg = self._settings.soap_service(service_key)
        metrics = self._telemetry.soap_metrics
        started = time.perf_counter()
        attempts = 0
        outcome: str | None = "unexpected"

        with (
            soap_scope(service_key, op_name),
            self._telemetry.tracer.start_as_current_span(
                f"SOAP {service_key}/{op_name}",
                kind=SpanKind.INTERNAL,
                attributes={
                    "soap.service": service_key,
                    "soap.operation": op_name,
                    "enduser.id": credentials.username,
                },
                record_exception=False,
                set_status_on_exception=False,
            ) as span,
        ):
            try:
                document = self._wsdl.get(operation.service, cfg)
                address = self._resolve_address(document, operation, cfg)
                args = build_request_args(request)
                while True:
                    attempts += 1
                    try:
                        result = self._attempt(
                            operation, cfg, document, address, args, credentials, attempts
                        )
                    except AppError as exc:
                        if isinstance(exc, SoapTimeoutError):
                            metrics.record_timeout(service_key, op_name)
                        delay = self._retry_delay(exc, operation, cfg, attempts)
                        if delay is None:
                            raise
                        metrics.record_retry(service_key, op_name, exc.error_type)
                        span.add_event(
                            "soap.retry",
                            {
                                "soap.attempt": attempts,
                                "error.type": exc.error_type,
                                "retry.delay_ms": round(delay * 1000),
                            },
                        )
                        self._sleep(delay)
                    else:
                        outcome = None
                        return result
            except AppError as exc:
                outcome = exc.error_type
                raise
            finally:
                elapsed = time.perf_counter() - started
                span.set_attribute("soap.attempts", attempts)
                span.set_attribute("soap.outcome", outcome or "success")
                if outcome is not None:
                    span.set_attribute("error.type", outcome)
                    span.set_status(Status(StatusCode.ERROR, outcome))
                metrics.record_call(service_key, op_name, elapsed, outcome)
                self._log_outcome(elapsed, attempts, outcome)

    def _retry_delay(
        self,
        error: AppError,
        operation: SoapOperation[Any, Any],
        cfg: SoapServiceSettings,
        attempt: int,
    ) -> float | None:
        if attempt >= cfg.retry.max_attempts or not is_retryable(
            error, idempotent=operation.idempotent
        ):
            return None
        return backoff_delay(attempt, cfg.retry, self._rng)

    @staticmethod
    def _log_outcome(elapsed: float, attempts: int, error_type: str | None) -> None:
        extra: dict[str, object] = {
            "duration_ms": round(elapsed * 1000, 1),
            "soap.attempts": attempts,
            "soap.outcome": error_type or "success",
        }
        if error_type is None:
            logger.info("soap call completed", extra=extra)
        else:
            logger.warning("soap call failed", extra={**extra, "error.type": error_type})

    def _resolve_address(
        self, document: Document, operation: SoapOperation[Any, Any], cfg: SoapServiceSettings
    ) -> str:
        address = cfg.endpoint_url or _wsdl_address(document, operation.service.binding_qname)
        if address is None:
            raise SoapWsdlUnavailableError()
        if (
            not address.startswith("https://")
            and self._settings.environment not in _INSECURE_ALLOWED
        ):
            logger.error(
                "endpoint SOAP sem TLS recusado fora de local/test",
                extra={"soap.service": operation.service.key, "error.type": "insecure_endpoint"},
            )
            raise SoapWsdlUnavailableError()
        return address

    def _attempt[Req: SoapModel, Resp: SoapModel](
        self,
        operation: SoapOperation[Req, Resp],
        cfg: SoapServiceSettings,
        document: Document,
        address: str,
        args: dict[str, Any],
        credentials: BasicCredentials,
        attempt: int,
    ) -> Resp:
        with self._telemetry.tracer.start_as_current_span(
            "soap.attempt",
            attributes={"soap.attempt": attempt},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            session = build_request_session(cfg, credentials)
            transport = RequestTransport(
                session,
                address=address,
                connect_timeout=cfg.connect_timeout_seconds,
                read_timeout=cfg.read_timeout_seconds,
            )
            capture = _ResponseCapture()
            wsse = (
                UsernameToken(credentials.username, credentials.password)
                if cfg.auth_mode is SoapAuthMode.WS_SECURITY_USERNAME_TOKEN
                else None
            )
            try:
                client = Client(
                    document,
                    wsse=wsse,
                    transport=transport,
                    plugins=[capture],
                    settings=hardened_zeep_settings(),
                )
                service = client.create_service(operation.service.binding_qname, address)
                try:
                    service[operation.name](**args)
                    return self._parse_response(operation, client, capture)
                except AppError:
                    raise
                except Exception as exc:
                    raise self._translate(exc, operation, client, transport, capture) from None
            except AppError as exc:
                span.set_status(Status(StatusCode.ERROR, exc.error_type))
                span.set_attribute("error.type", exc.error_type)
                raise
            finally:
                session.close()

    @staticmethod
    def _parse_response[Req: SoapModel, Resp: SoapModel](
        operation: SoapOperation[Req, Resp], client: Client, capture: _ResponseCapture
    ) -> Resp:
        body = capture.body_child()
        if body is None or body.tag != operation.response_element_qname:
            raise SoapMalformedResponseError()
        value = client.get_element(operation.response_element_qname).parse(body, client.wsdl.types)
        return operation.response_model.from_plain(to_plain(value, body))

    @staticmethod
    def _translate(
        exc: Exception,
        operation: SoapOperation[Any, Any],
        client: Client,
        transport: RequestTransport,
        capture: _ResponseCapture,
    ) -> AppError:
        """Converte exceções de transporte/Zeep em erros internos tipados, sem reter conteúdo."""
        if isinstance(exc, UpstreamHttpError):
            if exc.status_code == 401:
                return AuthenticationFailedError()
            if exc.status_code == 403:
                return AuthorizationError()
            return SoapTransportError(upstream_status=exc.status_code)
        if isinstance(exc, LocationNotAllowedError):
            return SoapWsdlUnavailableError()
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return SoapTimeoutError(request_sent=False)
        if isinstance(exc, requests.exceptions.Timeout):
            return SoapTimeoutError(request_sent=True)
        if isinstance(exc, requests.exceptions.SSLError):
            return SoapConnectionError(request_sent=False, transient=False)
        if isinstance(exc, requests.exceptions.ConnectionError):
            return SoapConnectionError(request_sent=not _connect_phase_failure(exc))
        if isinstance(exc, requests.exceptions.RequestException):
            return SoapConnectionError(request_sent=True)
        if isinstance(exc, Fault):
            if not capture.has_soap_fault():
                return SoapTransportError(upstream_status=500)
            return _fault_error(exc, operation, client)
        if isinstance(exc, TransportError):
            if exc.status_code in (401, 403):
                return (
                    AuthenticationFailedError() if exc.status_code == 401 else AuthorizationError()
                )
            if exc.status_code == 200 or not exc.status_code:
                return SoapMalformedResponseError()
            return SoapTransportError(upstream_status=exc.status_code)
        if isinstance(exc, ZeepError | etree.XMLSyntaxError):
            return (
                SoapMalformedResponseError()
                if transport.response_received
                else SoapSerializationError()
            )
        if not transport.response_received and isinstance(exc, TypeError | ValueError):
            return SoapSerializationError()
        raise exc


def _fault_error(
    fault: Fault, operation: SoapOperation[Any, Any], client: Client
) -> SoapFaultError:
    detail = None
    qname = operation.fault_element_qname
    node = fault.detail
    if operation.fault_model is not None and qname is not None and isinstance(node, etree._Element):
        child = node.find(qname)
        if child is not None:
            try:
                parsed = client.get_element(qname).parse(child, client.wsdl.types)
                detail = operation.fault_model.from_plain(to_plain(parsed, child))
            except (ZeepError, ContractMappingError):
                detail = None
    return SoapFaultError(fault_code=fault.code, fault_message=fault.message, detail=detail)


def _wsdl_address(document: Document, binding_qname: str) -> str | None:
    for service in document.services.values():
        for port in service.ports.values():
            if str(port.binding.name) == binding_qname:
                address = port.binding_options.get("address")
                return str(address) if address else None
    return None
