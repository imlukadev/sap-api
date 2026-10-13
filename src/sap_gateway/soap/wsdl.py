"""Carga única e cache dos metadados SOAP (WSDL + XSD).

Estratégia de isolamento: o ``zeep.wsdl.Document`` resultante é somente-leitura após a carga e
**não contém credenciais de usuário** (a carga usa, no máximo, uma credencial técnica configurada
por ambiente). Ele é compartilhado entre requisições; todo estado de autenticação, sessão HTTP e
cookies vive em ``Client``/``Session`` criados por chamada (ver ``gateway.py``).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from lxml import etree
from zeep import Settings as ZeepSettings
from zeep.exceptions import Error as ZeepError
from zeep.wsdl import Document

from sap_gateway.config import SoapServiceSettings
from sap_gateway.errors import SoapWsdlUnavailableError
from sap_gateway.soap.contracts import SoapServiceSpec
from sap_gateway.soap.transport import LoadTransport, LocationNotAllowedError

logger = logging.getLogger(__name__)


def hardened_zeep_settings(*, loading: bool = False) -> ZeepSettings:
    """Zeep por padrão não proíbe DTD/entidades externas; fechamos explicitamente (anti-XXE).

    ``forbid_external`` também bloqueia ``xsd:import`` http(s) de um WSDL remoto. Na *carga* ele é
    desligado e a restrição passa a ser feita por ``LoadTransport`` (hosts da configuração);
    nas chamadas (parse de respostas) permanece ligado.
    """
    return ZeepSettings(
        strict=True,
        raw_response=False,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=not loading,
        xml_huge_tree=False,
    )


class WsdlStore:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._lock = threading.Lock()

    def get(self, spec: SoapServiceSpec, cfg: SoapServiceSettings) -> Document:
        document = self._documents.get(spec.key)
        if document is not None:
            return document
        with self._lock:
            document = self._documents.get(spec.key)
            if document is None:
                document = self._load(spec, cfg)
                self._documents[spec.key] = document
            return document

    def is_loaded(self, key: str) -> bool:
        return key in self._documents

    @staticmethod
    def _load(spec: SoapServiceSpec, cfg: SoapServiceSettings) -> Document:
        location = cfg.wsdl or str(spec.packaged_wsdl_path())
        parsed = urlparse(location)
        extra_hosts = {host.lower() for host in cfg.allowed_hosts}
        if parsed.scheme in {"http", "https"}:
            hosts = frozenset({(parsed.hostname or "").lower(), *extra_hosts})
            root = None
        else:
            hosts = frozenset(extra_hosts)
            root = Path(location).expanduser().resolve().parent
            location = str(Path(location).expanduser().resolve())
        transport = LoadTransport(cfg, allowed_hosts=hosts, root=root)
        try:
            # o stub do Zeep declara `type[Transport]`, mas a API aceita uma instância
            return Document(
                location,
                transport,  # type: ignore[arg-type]
                settings=hardened_zeep_settings(loading=True),
            )
        except (
            LocationNotAllowedError,
            requests.RequestException,
            OSError,
            ZeepError,
            etree.XMLSyntaxError,
        ) as exc:
            logger.error(
                "falha ao carregar WSDL/XSD",
                extra={
                    "soap.service": spec.key,
                    "error.type": "wsdl_unavailable",
                    "error.cause": type(exc).__name__,
                },
            )
            raise SoapWsdlUnavailableError() from None
