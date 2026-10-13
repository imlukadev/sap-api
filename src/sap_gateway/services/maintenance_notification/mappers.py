"""Mapeamentos explícitos REST ↔ SOAP.

* Códigos de domínio: tabelas bidirecionais completas (testadas para exaustividade).
* Campos opcionais: só são repassados quando **definidos** na origem (``model_fields_set``),
  preservando a diferença entre ausente, ``null`` e vazio.
* Estruturas incompatíveis falham explicitamente; nada é descartado em silêncio.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from sap_gateway.errors import ContractMappingError
from sap_gateway.services.maintenance_notification import contracts as soap
from sap_gateway.services.maintenance_notification import schemas as rest

NOTIFICATION_TYPE_TO_SOAP = {
    rest.NotificationType.MAINTENANCE_REQUEST: soap.NotificationTypeCode.M1,
    rest.NotificationType.MALFUNCTION_REPORT: soap.NotificationTypeCode.M2,
    rest.NotificationType.ACTIVITY_REPORT: soap.NotificationTypeCode.M3,
}
PRIORITY_TO_SOAP = {
    rest.Priority.VERY_HIGH: soap.PriorityCode.P1,
    rest.Priority.HIGH: soap.PriorityCode.P2,
    rest.Priority.MEDIUM: soap.PriorityCode.P3,
    rest.Priority.LOW: soap.PriorityCode.P4,
}
STATUS_TO_SOAP = {
    rest.NotificationStatus.OUTSTANDING: soap.StatusCode.OSNO,
    rest.NotificationStatus.IN_PROCESS: soap.StatusCode.NOPR,
    rest.NotificationStatus.COMPLETED: soap.StatusCode.NOCO,
}
SEVERITY_FROM_SOAP = {
    soap.MessageSeverity.S: rest.MessageSeverity.SUCCESS,
    soap.MessageSeverity.I: rest.MessageSeverity.INFO,
    soap.MessageSeverity.W: rest.MessageSeverity.WARNING,
    soap.MessageSeverity.E: rest.MessageSeverity.ERROR,
}

NOTIFICATION_TYPE_FROM_SOAP = {v: k for k, v in NOTIFICATION_TYPE_TO_SOAP.items()}
PRIORITY_FROM_SOAP = {v: k for k, v in PRIORITY_TO_SOAP.items()}
STATUS_FROM_SOAP = {v: k for k, v in STATUS_TO_SOAP.items()}


def _identity[T](value: T) -> T:
    return value


def _carry(
    source: BaseModel, fields: tuple[tuple[str, str, Callable[[Any], Any]], ...]
) -> dict[str, Any]:
    """Copia ``(campo_origem, campo_destino, conversor)`` apenas para campos definidos na origem."""
    out: dict[str, Any] = {}
    for src, dst, convert in fields:
        if src in source.model_fields_set:
            value = getattr(source, src)
            out[dst] = None if value is None else convert(value)
    return out


def _lookup[K, V](table: dict[K, V], key: K) -> V:
    try:
        return table[key]
    except KeyError:
        raise ContractMappingError() from None


# --- REST → SOAP ---------------------------------------------------------------------------

_HEADER_OPTIONAL_TO_SOAP = (
    ("priority", "priority", lambda v: PRIORITY_TO_SOAP[v]),
    ("functional_location", "functional_location", _identity),
    ("reported_by", "reported_by", _identity),
    ("required_start", "required_start", _identity),
    ("planned_cost", "planned_cost", _identity),
)
_ITEM_OPTIONAL_TO_SOAP = (
    ("description", "description", _identity),
    ("damage_code", "damage_code", _identity),
    ("quantity", "quantity", _identity),
)


def create_request_to_soap(body: rest.NotificationCreate) -> soap.NotificationCreateRequest:
    header = soap.NotificationHeader(
        notification_type=NOTIFICATION_TYPE_TO_SOAP[body.notification_type],
        description=body.description,
        plant=body.plant,
        **_carry(body, _HEADER_OPTIONAL_TO_SOAP),
    )
    optional: dict[str, Any] = {}
    if "items" in body.model_fields_set:
        optional["items"] = soap.NotificationItemList(
            item=[
                soap.NotificationItem(
                    item_number=item.item_number, **_carry(item, _ITEM_OPTIONAL_TO_SOAP)
                )
                for item in body.items
            ]
        )
    if "long_text" in body.model_fields_set:
        optional["long_text"] = soap.LongText(text_line=list(body.long_text))
    if "attributes" in body.model_fields_set:
        optional["attributes"] = soap.AttributeList(
            attribute=[soap.Attribute(name=a.name, value=a.value) for a in body.attributes]
        )
    return soap.NotificationCreateRequest(header=header, **optional)


def search_query_to_soap(query: rest.NotificationSearchQuery) -> soap.NotificationSearchRequest:
    return soap.NotificationSearchRequest(
        max_results=query.max_results,
        **_carry(
            query,
            (
                ("plant", "plant", _identity),
                ("status", "status", lambda v: STATUS_TO_SOAP[v]),
                ("created_from", "created_from", _identity),
                ("created_to", "created_to", _identity),
            ),
        ),
    )


# --- SOAP → REST ---------------------------------------------------------------------------

_HEADER_OPTIONAL_FROM_SOAP = (
    ("priority", "priority", lambda v: _lookup(PRIORITY_FROM_SOAP, v)),
    ("functional_location", "functional_location", _identity),
    ("reported_by", "reported_by", _identity),
    ("required_start", "required_start", _identity),
    ("planned_cost", "planned_cost", _identity),
)
_ITEM_OPTIONAL_FROM_SOAP = _ITEM_OPTIONAL_TO_SOAP


def notification_from_soap(data: soap.NotificationData) -> rest.NotificationOut:
    header = data.header
    return rest.NotificationOut(
        notification_id=data.notification_id,
        notification_type=_lookup(NOTIFICATION_TYPE_FROM_SOAP, header.notification_type),
        description=header.description,
        plant=header.plant,
        status=_lookup(STATUS_FROM_SOAP, data.status),
        created_on=data.created_on,
        created_at=data.created_at,
        items=[
            rest.ItemOut(item_number=item.item_number, **_carry(item, _ITEM_OPTIONAL_FROM_SOAP))
            for item in (data.items.item if data.items else [])
        ],
        long_text=list(data.long_text.text_line) if data.long_text else [],
        attributes=[
            rest.AttributeOut(name=a.name, value=a.value)
            for a in (data.attributes.attribute if data.attributes else [])
        ],
        **_carry(header, _HEADER_OPTIONAL_FROM_SOAP),
    )


def created_from_soap(response: soap.NotificationCreateResponse) -> rest.NotificationCreated:
    messages = response.messages.message if response.messages else []
    return rest.NotificationCreated(
        notification_id=response.notification_id,
        messages=[
            rest.MessageOut(severity=_lookup(SEVERITY_FROM_SOAP, m.severity), text=m.text)
            for m in messages
        ],
    )


def search_result_from_soap(
    response: soap.NotificationSearchResponse,
) -> rest.NotificationSearchResult:
    summaries = response.notifications.notification if response.notifications else []
    return rest.NotificationSearchResult(
        notifications=[
            rest.NotificationSummaryOut(
                notification_id=s.notification_id,
                description=s.description,
                status=_lookup(STATUS_FROM_SOAP, s.status),
                created_on=s.created_on,
                **_carry(s, (("priority", "priority", lambda v: _lookup(PRIORITY_FROM_SOAP, v)),)),
            )
            for s in summaries
        ],
        truncated=response.truncated,
    )
