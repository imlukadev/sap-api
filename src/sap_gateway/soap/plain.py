"""Conversão de objetos Zeep em estruturas simples, **preservando ausência, nil e vazio**.

O Zeep devolve ``None`` para elemento ausente, ``xsi:nil="true"`` e elemento vazio. Para não
perder essa distinção, percorremos em paralelo o valor Zeep (tipado) e o XML original da
resposta, usando os metadados de tipo do próprio Zeep (``_xsd_type.elements``):

* elemento ausente      → chave não existe;
* ``xsi:nil="true"``    → ``None``;
* elemento vazio        → ``""`` (simples) ou ``{}`` (complexo);
* demais                → valor já tipado pelo Zeep (``date``, ``Decimal``, ``int``, ...).
"""

from __future__ import annotations

from typing import Any

from lxml import etree
from zeep.xsd import CompoundValue
from zeep.xsd.types.complex import ComplexType

from sap_gateway.errors import ContractMappingError

_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def to_plain(value: CompoundValue, node: etree._Element) -> dict[str, Any]:
    """``value`` é o objeto Zeep do elemento ``node`` (ex.: o wrapper ``...Response``)."""
    out: dict[str, Any] = {}
    for name, element in value._xsd_type.elements:
        children = node.findall(element.qname.text)
        if not children:
            continue
        python_value = value.__values__.get(name)
        if element.max_occurs == 1:
            if len(children) != 1:
                raise ContractMappingError()
            out[name] = _convert(python_value, children[0], element)
            continue
        items = python_value if isinstance(python_value, list) else []
        if len(items) != len(children):
            raise ContractMappingError()
        out[name] = [
            _convert(py, child, element) for py, child in zip(items, children, strict=True)
        ]
    return out


def _convert(python_value: object, node: etree._Element, element: Any) -> object:
    if node.get(_XSI_NIL) in {"true", "1"}:
        return None
    if isinstance(python_value, CompoundValue):
        return to_plain(python_value, node)
    if python_value is None:
        if isinstance(element.type, ComplexType):
            return {}
        if len(node) == 0 and not (node.text or "").strip():
            return ""
        raise ContractMappingError()
    if isinstance(python_value, etree._Element | list | dict):
        raise ContractMappingError()
    return python_value
