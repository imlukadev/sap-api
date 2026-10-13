"""Contrato SOAP do serviço ``MaintenanceNotification`` (espelha ``maintenance_notification.xsd``).

Nomes Python em snake_case; aliases (gerados em PascalCase pela base ``SoapModel``) correspondem
aos elementos do XSD. Campos ``X | None = None`` são elementos ``minOccurs=0``/``nillable``;
a presença no XML é controlada por ``model_fields_set``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from sap_gateway.soap.contracts import SoapModel, SoapOperation, SoapServiceSpec

NAMESPACE = "urn:example:sap:pm:notification:v1"

SERVICE = SoapServiceSpec(
    key="maintenance_notification",
    namespace=NAMESPACE,
    binding="MaintenanceNotificationBinding",
    default_wsdl="maintenance_notification.wsdl",
)

NOTIFICATION_ID_PATTERN = r"^[0-9]{12}$"


class NotificationTypeCode(StrEnum):
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"


class PriorityCode(StrEnum):
    P1 = "1"
    P2 = "2"
    P3 = "3"
    P4 = "4"


class StatusCode(StrEnum):
    OSNO = "OSNO"
    NOPR = "NOPR"
    NOCO = "NOCO"


class MessageSeverity(StrEnum):
    S = "S"
    I = "I"  # noqa: E741 - código de domínio do XSD
    W = "W"
    E = "E"


class NotificationHeader(SoapModel):
    notification_type: NotificationTypeCode
    description: str = Field(max_length=40)
    priority: PriorityCode | None = None
    plant: str = Field(min_length=4, max_length=4)
    functional_location: str | None = None
    reported_by: str | None = None
    required_start: date | None = None
    planned_cost: Decimal | None = None


class NotificationItem(SoapModel):
    item_number: int
    description: str | None = None
    damage_code: str | None = None
    quantity: Decimal | None = None


class NotificationItemList(SoapModel):
    item: list[NotificationItem] = Field(default_factory=list)


class LongText(SoapModel):
    text_line: list[str] = Field(default_factory=list)


class Attribute(SoapModel):
    name: str
    value: str | None


class AttributeList(SoapModel):
    attribute: list[Attribute] = Field(default_factory=list)


class NotificationData(SoapModel):
    notification_id: str = Field(pattern=NOTIFICATION_ID_PATTERN)
    header: NotificationHeader
    status: StatusCode
    created_on: date
    created_at: datetime
    items: NotificationItemList | None = None
    long_text: LongText | None = None
    attributes: AttributeList | None = None


class NotificationSummary(SoapModel):
    notification_id: str = Field(pattern=NOTIFICATION_ID_PATTERN)
    description: str
    status: StatusCode
    priority: PriorityCode | None = None
    created_on: date


class Message(SoapModel):
    severity: MessageSeverity
    text: str


class MessageList(SoapModel):
    message: list[Message] = Field(default_factory=list)


class NotificationSummaryList(SoapModel):
    notification: list[NotificationSummary] = Field(default_factory=list)


class NotificationReadRequest(SoapModel):
    notification_id: str = Field(pattern=NOTIFICATION_ID_PATTERN)


class NotificationReadResponse(SoapModel):
    notification: NotificationData


class NotificationCreateRequest(SoapModel):
    header: NotificationHeader
    items: NotificationItemList | None = None
    long_text: LongText | None = None
    attributes: AttributeList | None = None


class NotificationCreateResponse(SoapModel):
    notification_id: str = Field(pattern=NOTIFICATION_ID_PATTERN)
    messages: MessageList | None = None


class NotificationSearchRequest(SoapModel):
    plant: str | None = None
    status: StatusCode | None = None
    created_from: date | None = None
    created_to: date | None = None
    max_results: int | None = None


class NotificationSearchResponse(SoapModel):
    notifications: NotificationSummaryList | None = None
    truncated: bool


class NotificationFault(SoapModel):
    code: str
    message: str


NOTIFICATION_READ = SoapOperation(
    service=SERVICE,
    name="NotificationRead",
    request_model=NotificationReadRequest,
    response_model=NotificationReadResponse,
    idempotent=True,
    fault_model=NotificationFault,
    fault_element="NotificationFault",
)

NOTIFICATION_SEARCH = SoapOperation(
    service=SERVICE,
    name="NotificationSearch",
    request_model=NotificationSearchRequest,
    response_model=NotificationSearchResponse,
    idempotent=True,
    fault_model=NotificationFault,
    fault_element="NotificationFault",
)

NOTIFICATION_CREATE = SoapOperation(
    service=SERVICE,
    name="NotificationCreate",
    request_model=NotificationCreateRequest,
    response_model=NotificationCreateResponse,
    idempotent=False,
    fault_model=NotificationFault,
    fault_element="NotificationFault",
)
