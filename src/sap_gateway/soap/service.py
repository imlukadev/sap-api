"""``SoapService``: encapsulador de um serviço SOAP sobre o Zeep.

    catalog = SoapService("catalog", "{urn:ns}Binding", settings, default_wsdl=path_do_wsdl)
    result = catalog.call("OperationName", credentials, selection=Model(...), idempotent=True)

Isolamento: o WSDL é parseado uma vez e reaproveitado (somente leitura). Já ``Session`` →
``Transport`` → ``Client`` são criados a cada tentativa, com a credencial do usuário, e a sessão é
fechada ao final: nada de autenticação ou cookie é compartilhado entre chamadas.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, overload

import requests
from lxml import etree
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import BaseModel, ValidationError
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, NewConnectionError
from zeep import Client, Settings
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import Fault, TransportError
from zeep.helpers import serialize_object
from zeep.wsdl import Document
from zeep.wsse.username import UsernameToken

from sap_gateway.auth import BasicCredentials
from sap_gateway.config import SoapAuthMode, SoapServiceSettings
from sap_gateway.errors import AppError, AuthenticationFailedError, AuthorizationError, ErrorDetail
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.soap.errors import (
    SoapConnectionError,
    SoapFaultError,
    SoapHttpError,
    SoapRequestError,
    SoapResponseError,
    SoapTimeoutError,
    SoapUnavailableError,
)
from sap_gateway.soap.models import SapModel
from sap_gateway.soap.retry import backoff_delay, is_retryable
from sap_gateway.soap.transport import RequestTransport, UpstreamHttpError, build_session

logger = logging.getLogger(__name__)


def zeep_settings(settings: SoapServiceSettings, *, loading: bool = False) -> Settings:
    """Zeep não proíbe DTD/entidades por padrão; fechamos (anti-XXE).

    ``forbid_external`` bloqueia ``xsd:import`` http(s); fica desligado só na carga do WSDL.
    """
    return Settings(
        strict=settings.strict,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=not loading,
        xml_huge_tree=False,
    )


class SoapService:
    def __init__(
        self,
        name: str,
        binding: str,
        settings: SoapServiceSettings,
        *,
        default_wsdl: str | Path | None = None,
        telemetry: Telemetry | None = None,
        require_https: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """``binding``: ``{namespace}NomeDoBinding``. ``require_https=False`` só em local/test."""
        self.name = name
        self._binding = binding
        self._cfg = settings
        self._wsdl_location = settings.wsdl or str(default_wsdl or "")
        self._telemetry = telemetry
        self._require_https = require_https
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, não criptografia
        self._document: Document | None = None
        self._lock = threading.Lock()

    @property
    def contract_loaded(self) -> bool:
        return self._document is not None

    # ------------------------------------------------------------------ API pública

    @overload
    def call[T: BaseModel](
        self,
        operation: str,
        credentials: BasicCredentials,
        payload: SapModel | dict[str, Any] | None = ...,
        *,
        response_model: type[T],
        idempotent: bool = ...,
        **kwargs: Any,
    ) -> T: ...

    @overload
    def call(
        self,
        operation: str,
        credentials: BasicCredentials,
        payload: SapModel | dict[str, Any] | None = ...,
        *,
        response_model: None = ...,
        idempotent: bool = ...,
        **kwargs: Any,
    ) -> Any: ...

    def call(
        self,
        operation: str,
        credentials: BasicCredentials,
        payload: SapModel | dict[str, Any] | None = None,
        *,
        response_model: type[BaseModel] | None = None,
        idempotent: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Executa ``operation`` com as credenciais do usuário.

        Argumentos: ``payload`` (``SapModel`` de requisição ou dict) e/ou ``**kwargs`` com os nomes
        XML da operação; ``SapModel`` aninhados são convertidos (aliases; ``None`` é omitido).
        Com ``response_model`` o resultado é validado nesse modelo; sem ele, devolve o objeto do
        Zeep. ``idempotent=True`` autoriza retry também após o envio (timeout de leitura,
        502/503/504); o padrão (False) é seguro para criações.
        """
        args = _soap_args(payload, kwargs)
        started = time.perf_counter()
        attempts = 0
        outcome: str | None = "unexpected"
        attributes = {
            "soap.service": self.name,
            "soap.operation": operation,
            "enduser.id": credentials.username,
        }
        with self._span(f"SOAP {self.name}/{operation}", attributes) as span:
            try:
                document = self._load_document(credentials)
                address = self._address(document)
                while True:
                    attempts += 1
                    try:
                        result = self._attempt(
                            operation,
                            document,
                            address,
                            args,
                            credentials,
                            response_model,
                            attempts,
                        )
                    except AppError as exc:
                        delay = self._retry_delay(exc, idempotent, attempts)
                        self._record_failed_attempt(exc, operation, delay, span)
                        if delay is None:
                            raise
                        self._sleep(delay)
                    else:
                        outcome = None
                        return result
            except AppError as exc:
                outcome = exc.error_type
                raise
            finally:
                self._finish(operation, started, attempts, outcome, span)

    def xml_request(
        self, operation: str, payload: SapModel | dict[str, Any] | None = None, **kwargs: Any
    ) -> str:
        """XML da requisição (para debug/testes). Não envia nada nem usa credenciais."""
        document = self._load_document(None)
        client = Client(document, settings=zeep_settings(self._cfg))
        service = client.create_service(self._binding, self._address(document))
        args = _soap_args(payload, kwargs)
        envelope = client.create_message(service, operation, **args)
        return etree.tostring(envelope, pretty_print=True, encoding="unicode")

    # ------------------------------------------------------------------ WSDL

    def _load_document(self, credentials: BasicCredentials | None) -> Document:
        """Parseia o WSDL uma vez (falhas não são cacheadas).

        WSDL por URL costuma exigir login: usa ``wsdl_username/password`` se configurados, senão a
        credencial do primeiro chamador — numa sessão descartada logo após a carga.
        """
        if self._document is not None:
            return self._document
        with self._lock:
            if self._document is None:
                self._document = self._parse_wsdl(credentials)
            return self._document

    def _parse_wsdl(self, credentials: BasicCredentials | None) -> Document:
        from zeep.transports import Transport

        cfg = self._cfg
        technical = (
            BasicCredentials(cfg.wsdl_username, cfg.wsdl_password.get_secret_value())
            if cfg.wsdl_username and cfg.wsdl_password
            else None
        )
        is_url = self._wsdl_location.startswith(("http://", "https://"))
        session = build_session(cfg, (technical or credentials) if is_url else None)
        try:
            transport = Transport(session=session, timeout=cfg.connect_timeout_seconds + 10)
            return Document(
                self._wsdl_location,
                transport,  # type: ignore[arg-type]
                settings=zeep_settings(cfg, loading=True),
            )
        except (requests.RequestException, OSError, ZeepError, etree.XMLSyntaxError) as exc:
            logger.error(
                "falha ao carregar WSDL/XSD",
                extra={
                    "soap.service": self.name,
                    "error.type": "wsdl_unavailable",
                    "error.cause": type(exc).__name__,
                },
            )
            raise SoapUnavailableError() from None
        finally:
            session.auth = None
            session.close()

    def _address(self, document: Document) -> str:
        address = self._cfg.endpoint_url or _wsdl_address(document, self._binding)
        if address is None or (self._require_https and not address.startswith("https://")):
            logger.error(
                "endpoint SOAP ausente ou sem TLS recusado",
                extra={"soap.service": self.name, "error.type": "insecure_endpoint"},
            )
            raise SoapUnavailableError()
        return address

    # ------------------------------------------------------------------ uma tentativa

    def _attempt(
        self,
        operation: str,
        document: Document,
        address: str,
        args: dict[str, Any],
        credentials: BasicCredentials,
        response_model: type[BaseModel] | None,
        attempt: int,
    ) -> Any:
        cfg = self._cfg
        session = build_session(cfg, credentials)
        transport = RequestTransport(
            session, timeouts=(cfg.connect_timeout_seconds, cfg.read_timeout_seconds)
        )
        wsse = (
            UsernameToken(credentials.username, credentials.password)
            if cfg.auth_mode is SoapAuthMode.WS_SECURITY_USERNAME_TOKEN
            else None
        )
        with self._span("soap.attempt", {"soap.attempt": attempt}) as span:
            try:
                client = Client(
                    document, wsse=wsse, transport=transport, settings=zeep_settings(cfg)
                )
                service = client.create_service(self._binding, address)
                try:
                    return _to_model(service[operation](**args), response_model)
                except AppError:
                    raise
                except Exception as exc:
                    raise _translate(exc, transport) from None
            except AppError as exc:
                if span:
                    span.set_status(Status(StatusCode.ERROR, exc.error_type))
                    span.set_attribute("error.type", exc.error_type)
                raise
            finally:
                session.close()

    @contextmanager
    def _span(self, name: str, attributes: dict[str, Any]) -> Iterator[Span | None]:
        """Span próprio (sem registrar mensagens de exceção); ``None`` sem telemetria."""
        if self._telemetry is None:
            yield None
            return
        with self._telemetry.tracer.start_as_current_span(
            name, attributes=attributes, record_exception=False, set_status_on_exception=False
        ) as span:
            yield span

    # ------------------------------------------------------------------ retry e telemetria

    def _retry_delay(self, error: AppError, idempotent: bool, attempt: int) -> float | None:
        policy = self._cfg.retry
        if attempt >= policy.max_attempts or not is_retryable(error, idempotent=idempotent):
            return None
        return backoff_delay(attempt, policy, self._rng)

    def _record_failed_attempt(
        self, error: AppError, operation: str, delay: float | None, span: Span | None
    ) -> None:
        metrics = self._telemetry.soap_metrics if self._telemetry else None
        if isinstance(error, SoapTimeoutError) and metrics:
            metrics.record_timeout(self.name, operation)
        if delay is not None and metrics:
            metrics.record_retry(self.name, operation, error.error_type)
        if delay is not None and span:
            span.add_event(
                "soap.retry",
                {"error.type": error.error_type, "retry.delay_ms": round(delay * 1000)},
            )

    def _finish(
        self, operation: str, started: float, attempts: int, outcome: str | None, span: Span | None
    ) -> None:
        elapsed = time.perf_counter() - started
        if span:
            span.set_attribute("soap.attempts", attempts)
            span.set_attribute("soap.outcome", outcome or "success")
            if outcome:
                span.set_attribute("error.type", outcome)
                span.set_status(Status(StatusCode.ERROR, outcome))
        if self._telemetry:
            self._telemetry.soap_metrics.record_call(self.name, operation, elapsed, outcome)
        extra: dict[str, object] = {
            "soap.service": self.name,
            "soap.operation": operation,
            "duration_ms": round(elapsed * 1000, 1),
            "soap.attempts": attempts,
            "soap.outcome": outcome or "success",
        }
        if outcome is None:
            logger.info("soap call completed", extra=extra)
        else:
            logger.warning("soap call failed", extra={**extra, "error.type": outcome})


# ---------------------------------------------------------------------- funções auxiliares


def _soap_args(payload: SapModel | dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    base = payload.to_soap() if isinstance(payload, SapModel) else dict(payload or {})
    return {
        key: value.to_soap() if isinstance(value, SapModel) else value
        for key, value in {**base, **kwargs}.items()
    }


def _to_model(result: Any, response_model: type[BaseModel] | None) -> Any:
    if response_model is None:
        return result
    try:
        return response_model.model_validate(serialize_object(result, target_cls=dict))
    except ValidationError as exc:
        raise SoapResponseError(
            details=[
                ErrorDetail(field=".".join(str(p) for p in err["loc"]), message=err["type"])
                for err in exc.errors(include_input=False, include_url=False)
            ]
        ) from None


def _connect_phase_failure(exc: requests.exceptions.ConnectionError) -> bool:
    """True se a falha foi anterior ao envio (recusa, DNS, timeout de conexão)."""
    for arg in exc.args:
        reason = arg.reason if isinstance(arg, MaxRetryError) else arg
        if isinstance(reason, NewConnectionError | ConnectTimeoutError):
            return True
    return False


def _translate(exc: Exception, transport: RequestTransport) -> AppError:
    """Converte exceções de transporte/Zeep em erros tipados, sem reter conteúdo da resposta."""
    if isinstance(exc, UpstreamHttpError):
        if exc.status_code == 401:
            return AuthenticationFailedError()
        if exc.status_code == 403:
            return AuthorizationError()
        return SoapHttpError(upstream_status=exc.status_code)
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
        return _fault_error(exc)
    if isinstance(exc, TransportError):
        if exc.status_code == 200 or not exc.status_code:
            return SoapResponseError()
        return SoapHttpError(upstream_status=exc.status_code)
    if isinstance(exc, ZeepError | etree.XMLSyntaxError):
        return SoapResponseError() if transport.response_received else SoapRequestError()
    if not transport.response_received and isinstance(exc, TypeError | ValueError):
        return SoapRequestError()
    raise exc


def _fault_error(fault: Fault) -> SoapFaultError | SoapHttpError:
    detail = fault.detail
    if isinstance(detail, str | bytes):
        # O Zeep levanta Fault também para erro HTTP sem <Fault> (detail = corpo em texto).
        return SoapHttpError(upstream_status=500)
    return SoapFaultError(
        fault_code=fault.code,
        fault_message=fault.message,
        detail=_xml_to_dict(detail) if isinstance(detail, etree._Element) else None,
    )


def _xml_to_dict(node: etree._Element) -> dict[str, object]:
    out: dict[str, object] = {}
    for child in node:
        if not isinstance(child.tag, str):
            continue
        name = etree.QName(child).localname
        out[name] = _xml_to_dict(child) if len(child) else (child.text or "").strip()
    return out


def _wsdl_address(document: Document, binding: str) -> str | None:
    for service in document.services.values():
        for port in service.ports.values():
            if str(port.binding.name) == binding:
                address = port.binding_options.get("address")
                return str(address) if address else None
    return None
