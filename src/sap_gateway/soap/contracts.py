"""Base dos contratos SOAP: modelos Pydantic ↔ argumentos Zeep, e descrição de operações.

Semântica preservada na ida e na volta (campo ausente ≠ ``None`` ≠ string vazia):

* campo **não definido** no modelo  → elemento omitido no XML;
* campo definido como ``None``      → ``xsi:nil="true"`` (somente em elementos ``nillable``);
* campo definido como ``""``        → elemento vazio.

Isso se apoia em ``model_fields_set`` do Pydantic; por isso os mapeadores só devem repassar
campos que o chamador realmente informou.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_pascal
from zeep.xsd import Nil

from sap_gateway.errors import ContractMappingError, ErrorDetail, SoapSerializationError


class SoapModel(BaseModel):
    """Modelo de um tipo complexo XSD. Nomes Python em snake_case, aliases = nomes do XSD."""

    model_config = ConfigDict(
        alias_generator=to_pascal,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        use_enum_values=False,
    )

    def to_soap_args(self) -> dict[str, Any]:
        """Converte para argumentos de tipo complexo do Zeep, respeitando o que foi definido."""
        args: dict[str, Any] = {}
        fields = type(self).model_fields
        for name, info in fields.items():
            if name not in self.model_fields_set:
                continue
            value = getattr(self, name)
            args[info.alias or name] = Nil if value is None else _to_soap_value(value)
        return args

    @classmethod
    def from_plain(cls, data: dict[str, Any]) -> Self:
        """Valida a estrutura devolvida por ``soap.plain.to_plain`` contra o contrato."""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ContractMappingError(
                details=[
                    ErrorDetail(field=".".join(str(p) for p in err["loc"]), message=err["type"])
                    for err in exc.errors(include_input=False, include_url=False)
                ]
            ) from None


def _to_soap_value(value: object) -> object:
    if isinstance(value, SoapModel):
        return value.to_soap_args()
    if isinstance(value, list | tuple):
        return [_to_soap_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def build_request_args(request: SoapModel) -> dict[str, Any]:
    try:
        return request.to_soap_args()
    except (TypeError, ValueError):
        raise SoapSerializationError() from None


@dataclass(frozen=True, slots=True)
class SoapServiceSpec:
    """Identifica um serviço SOAP (um binding de um WSDL)."""

    key: str
    """Chave estável: nome em ``APP_SOAP_SERVICES__<KEY>__*`` e valor de ``soap.service``."""
    namespace: str
    binding: str
    default_wsdl: str
    """Nome do arquivo WSDL empacotado em ``sap_gateway/wsdl`` usado quando não há override."""

    @property
    def binding_qname(self) -> str:
        return f"{{{self.namespace}}}{self.binding}"

    def packaged_wsdl_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "wsdl" / self.default_wsdl


@dataclass(frozen=True, slots=True)
class SoapOperation[Req: SoapModel, Resp: SoapModel]:
    """Contrato de uma operação publicada: nomes XML, modelos e política de retry."""

    service: SoapServiceSpec
    name: str
    request_model: type[Req]
    response_model: type[Resp]
    idempotent: bool
    """Somente operações idempotentes sofrem retry após falhas em que a requisição pode ter sido
    processada (timeout de leitura, HTTP 5xx)."""
    fault_model: type[SoapModel] | None = None
    fault_element: str | None = None

    @property
    def response_element_qname(self) -> str:
        return f"{{{self.service.namespace}}}{self.name}Response"

    @property
    def fault_element_qname(self) -> str | None:
        if self.fault_element is None:
            return None
        return f"{{{self.service.namespace}}}{self.fault_element}"
