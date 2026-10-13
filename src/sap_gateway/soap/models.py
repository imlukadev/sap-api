"""Base dos modelos de requisição/resposta SOAP.

Nomes Python em snake_case; o nome XML é o alias (PascalCase gerado automaticamente). Siglas que a
conversão automática erra (``ID``, ``URL``...) recebem ``Field(alias="ID")`` explícito. Use
``SoapService.xml_request(...)`` para conferir visualmente o XML gerado.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_pascal


class SapModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_pascal, populate_by_name=True, extra="ignore")

    def to_soap(self) -> dict[str, Any]:
        """Argumentos do Zeep: aliases XML, enums como valor, campos ``None`` omitidos."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="python")


SapStr = Annotated[str, BeforeValidator(lambda v: "" if v is None else v)]
"""String obrigatória que o SAP pode devolver vazia: o Zeep entrega ``None`` para ``<X/>``."""
