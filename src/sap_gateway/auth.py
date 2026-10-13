"""Recepção e validação de HTTP Basic no limite da API.

A API não possui base de usuários própria: o segredo é validado pelo serviço SOAP de destino.
Aqui garantimos apenas que o cabeçalho é bem formado e expomos as credenciais como um valor
imutável, entregue por *injeção de dependência* à rota — nunca guardado em estado global.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from opentelemetry import trace

from sap_gateway.context import set_username
from sap_gateway.errors import AuthenticationRequiredError

MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 256

_basic_scheme = HTTPBasic(
    auto_error=False,
    scheme_name="BasicAuth",
    description=(
        "Credenciais SAP do usuário. São encaminhadas ao serviço SOAP apenas durante esta "
        "requisição e descartadas ao final."
    ),
)


@dataclass(frozen=True, slots=True)
class BasicCredentials:
    username: str
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return f"BasicCredentials(username={self.username!r}, password='***')"

    __str__ = __repr__

    def __reduce__(self) -> tuple[Any, ...]:
        raise TypeError("BasicCredentials não pode ser serializado")


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def get_credentials(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
) -> BasicCredentials:
    """Dependência FastAPI: extrai e valida o formato das credenciais Basic."""
    if (
        credentials is None
        or not credentials.username
        or not credentials.password
        or len(credentials.username) > MAX_USERNAME_LENGTH
        or len(credentials.password) > MAX_PASSWORD_LENGTH
        or _has_control_chars(credentials.username)
        or _has_control_chars(credentials.password)
    ):
        raise AuthenticationRequiredError()

    set_username(credentials.username)
    trace.get_current_span().set_attribute("enduser.id", credentials.username)
    return BasicCredentials(credentials.username, credentials.password)


CurrentCredentials = Annotated[BasicCredentials, Depends(get_credentials)]
