"""Transporte HTTP do Zeep.

Dois transportes com responsabilidades distintas:

* ``LoadTransport``     – baixa WSDL/XSD uma única vez; restrito a hosts/diretórios autorizados
                          na configuração (anti-SSRF); jamais recebe credenciais de usuário;
* ``RequestTransport``  – criado **por tentativa de chamada**, dono de uma ``requests.Session``
                          exclusiva (credenciais, cookies) que é fechada ao final.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from requests.auth import HTTPBasicAuth
from zeep.transports import Transport

from sap_gateway.auth import BasicCredentials
from sap_gateway.config import SoapAuthMode, SoapServiceSettings


class UpstreamHttpError(Exception):
    """Resposta HTTP não-SOAP (redirect, 4xx, 5xx≠500) — o corpo é descartado sem ser lido."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class LocationNotAllowedError(Exception):
    """WSDL/XSD fora dos hosts ou diretórios autorizados pela configuração."""


def _requests_verify(cfg: SoapServiceSettings) -> bool | str:
    return str(cfg.tls.ca_bundle) if cfg.tls.ca_bundle else cfg.tls.verify


def _apply_common_session_settings(session: requests.Session, cfg: SoapServiceSettings) -> None:
    session.trust_env = False
    session.verify = _requests_verify(cfg)
    if cfg.tls.client_cert:
        session.cert = (
            (str(cfg.tls.client_cert), str(cfg.tls.client_key))
            if cfg.tls.client_key
            else str(cfg.tls.client_cert)
        )
    if cfg.proxy_url:
        session.proxies = {"http": cfg.proxy_url, "https": cfg.proxy_url}
    session.headers["User-Agent"] = "sap-gateway"


def build_request_session(
    cfg: SoapServiceSettings, credentials: BasicCredentials | None
) -> requests.Session:
    """Sessão nova e exclusiva de uma tentativa. Nunca reutilizada entre usuários."""
    session = requests.Session()
    _apply_common_session_settings(session, cfg)
    if credentials is not None and cfg.auth_mode is SoapAuthMode.HTTP_BASIC:
        session.auth = HTTPBasicAuth(credentials.username, credentials.password)
    return session


class RequestTransport(Transport):
    def __init__(
        self,
        session: requests.Session,
        *,
        address: str,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__(
            cache=None, session=session, operation_timeout=(connect_timeout, read_timeout)
        )
        self._address = address
        self.response_received = False

    def post(self, address: str, message: object, headers: dict[str, str]) -> requests.Response:
        if address != self._address:
            raise LocationNotAllowedError(address)
        response = self.session.post(
            address,
            data=message,
            headers=headers,
            timeout=self.operation_timeout,
            allow_redirects=False,
        )
        self.response_received = True
        status = response.status_code
        if status >= 300 and status != 500:
            response.close()
            raise UpstreamHttpError(status)
        return response

    def get(self, address: str, params: object, headers: object) -> requests.Response:
        raise LocationNotAllowedError(address)

    def load(self, url: str) -> bytes:
        raise LocationNotAllowedError(url)


class LoadTransport(Transport):
    """Carrega WSDL/XSD. Cada ``load`` usa uma sessão descartável; nada persiste entre cargas."""

    def __init__(
        self, cfg: SoapServiceSettings, *, allowed_hosts: frozenset[str], root: Path | None
    ):
        super().__init__(cache=None, timeout=cfg.wsdl_timeout_seconds)
        self._cfg = cfg
        self._allowed_hosts = allowed_hosts
        self._root = root

    def load(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            if (parsed.hostname or "").lower() not in self._allowed_hosts:
                raise LocationNotAllowedError(parsed.hostname or "")
            return self._fetch(url)
        if parsed.scheme not in {"", "file"}:
            raise LocationNotAllowedError(parsed.scheme)
        path = Path(unquote(parsed.path) if parsed.scheme == "file" else url).expanduser().resolve()
        if self._root is None or not path.is_relative_to(self._root):
            raise LocationNotAllowedError(path.name)
        return path.read_bytes()

    def _fetch(self, url: str) -> bytes:
        session = requests.Session()
        try:
            _apply_common_session_settings(session, self._cfg)
            if self._cfg.wsdl_username and self._cfg.wsdl_password:
                session.auth = HTTPBasicAuth(
                    self._cfg.wsdl_username, self._cfg.wsdl_password.get_secret_value()
                )
            response = session.get(url, timeout=self.load_timeout, allow_redirects=False)
            response.raise_for_status()
            return response.content
        finally:
            session.close()

    def post(self, address: str, message: object, headers: dict[str, str]) -> requests.Response:
        raise LocationNotAllowedError(address)
