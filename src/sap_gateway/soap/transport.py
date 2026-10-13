"""Sessão HTTP e transporte do Zeep. Ambos são criados POR TENTATIVA de chamada (ver service.py)."""

from __future__ import annotations

import requests
from requests.auth import HTTPBasicAuth
from zeep.transports import Transport

from sap_gateway.auth import BasicCredentials
from sap_gateway.config import SoapAuthMode, SoapServiceSettings


class UpstreamHttpError(Exception):
    """Resposta HTTP que não é SOAP (redirect, 4xx, 5xx≠500). O corpo é descartado sem leitura."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def build_session(
    cfg: SoapServiceSettings, credentials: BasicCredentials | None = None
) -> requests.Session:
    """Sessão nova (cookies e credenciais próprios). Sem proxy/netrc herdados do ambiente."""
    session = requests.Session()
    session.trust_env = False
    session.verify = str(cfg.tls.ca_bundle) if cfg.tls.ca_bundle else cfg.tls.verify
    if cfg.tls.client_cert:
        cert = str(cfg.tls.client_cert)
        session.cert = (cert, str(cfg.tls.client_key)) if cfg.tls.client_key else cert
    if cfg.proxy_url:
        session.proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url}
    session.headers["User-Agent"] = "sap-gateway"
    if credentials is not None and cfg.auth_mode is SoapAuthMode.HTTP_BASIC:
        session.auth = HTTPBasicAuth(credentials.username, credentials.password)
    return session


class RequestTransport(Transport):
    """POST com timeouts (conexão, leitura), sem redirects e sem ler corpo de erro HTTP."""

    def __init__(self, session: requests.Session, *, timeouts: tuple[float, float]) -> None:
        super().__init__(cache=None, session=session, operation_timeout=timeouts)
        self.response_received = False

    def post(self, address: str, message: object, headers: dict[str, str]) -> requests.Response:
        response = self.session.post(
            address,
            data=message,
            headers=headers,
            timeout=self.operation_timeout,
            allow_redirects=False,
        )
        self.response_received = True
        if response.status_code >= 300 and response.status_code != 500:
            response.close()
            raise UpstreamHttpError(response.status_code)
        return response
