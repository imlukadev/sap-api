"""Modelos SAP do serviço ``MaintenanceNotification`` (espelham ``maintenance_notification.xsd``).

Snake_case no Python, PascalCase no XML (alias automático). Códigos SAP ficam como ``str``;
a tradução para os enums públicos está em ``service.py``. Atenção: se o wrapper da resposta tem
um único filho, o Zeep devolve o próprio filho — por isso ``read`` valida ``NotificationData``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sap_gateway.soap.models import SapModel, SapStr

NAMESPACE = "urn:example:sap:pm:notification:v1"
BINDING = f"{{{NAMESPACE}}}MaintenanceNotificationBinding"
SERVICE_KEY = "maintenance_notification"


class Header(SapModel):
    notification_type: str
    description: SapStr
    plant: str
    priority: str | None = None
    functional_location: str | None = None
    reported_by: str | None = None
    required_start: date | None = None
    planned_cost: Decimal | None = None


class Item(SapModel):
    item_number: int
    description: str | None = None
    damage_code: str | None = None
    quantity: Decimal | None = None


class ItemList(SapModel):
    item: list[Item] = []


class LongText(SapModel):
    text_line: list[str] = []


class Attribute(SapModel):
    name: str
    value: str | None = None


class AttributeList(SapModel):
    attribute: list[Attribute] = []


class NotificationData(SapModel):
    notification_id: str
    header: Header
    status: str
    created_on: date
    created_at: datetime
    items: ItemList | None = None
    long_text: LongText | None = None
    attributes: AttributeList | None = None


class Summary(SapModel):
    notification_id: str
    description: SapStr
    status: str
    created_on: date
    priority: str | None = None


class SummaryList(SapModel):
    notification: list[Summary] = []


class SearchResponse(SapModel):
    notifications: SummaryList | None = None
    truncated: bool


class Message(SapModel):
    severity: str
    text: str


class MessageList(SapModel):
    message: list[Message] = []


class CreateResponse(SapModel):
    notification_id: str
    messages: MessageList | None = None


class ReadRequest(SapModel):
    notification_id: str


class SearchRequest(SapModel):
    max_results: int
    plant: str | None = None
    status: str | None = None
    created_from: date | None = None
    created_to: date | None = None


class CreateRequest(SapModel):
    header: Header
    items: ItemList | None = None
    long_text: LongText | None = None
    attributes: AttributeList | None = None
