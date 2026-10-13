"""Contrato REST público das notas de manutenção (JSON em snake_case, enums legíveis).

Independente do Zeep e dos códigos SAP: a tradução fica em ``service.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NotificationIdStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{12}$")]
PlantStr = Annotated[str, StringConstraints(min_length=4, max_length=4)]


class NotificationType(StrEnum):
    MAINTENANCE_REQUEST = "maintenance_request"
    MALFUNCTION_REPORT = "malfunction_report"
    ACTIVITY_REPORT = "activity_report"


class Priority(StrEnum):
    VERY_HIGH = "very_high"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NotificationStatus(StrEnum):
    OUTSTANDING = "outstanding"
    IN_PROCESS = "in_process"
    COMPLETED = "completed"


class MessageSeverity(StrEnum):
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemIn(RestModel):
    item_number: int = Field(ge=1, le=9999, examples=[1])
    description: str | None = Field(default=None, max_length=40)
    damage_code: str | None = Field(default=None, max_length=8)
    quantity: Decimal | None = Field(default=None, ge=0)


class AttributeIn(RestModel):
    name: str = Field(min_length=1, max_length=30)
    value: str | None = Field(max_length=255)


class NotificationCreate(RestModel):
    notification_type: NotificationType
    description: str = Field(min_length=1, max_length=40, examples=["Bomba P-101 vazando"])
    priority: Priority | None = None
    plant: PlantStr = Field(examples=["1000"])
    functional_location: str | None = Field(default=None, max_length=40)
    reported_by: str | None = Field(default=None, max_length=12)
    required_start: date | None = None
    planned_cost: Decimal | None = Field(default=None, ge=0)
    items: list[ItemIn] = Field(default_factory=list, max_length=100)
    long_text: list[str] = Field(default_factory=list, max_length=500)
    attributes: list[AttributeIn] = Field(default_factory=list, max_length=50)


class NotificationSearchQuery(RestModel):
    plant: PlantStr | None = None
    status: NotificationStatus | None = None
    created_from: date | None = None
    created_to: date | None = None
    max_results: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def _check_period(self) -> Self:
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from deve ser anterior ou igual a created_to")
        return self


class ItemOut(RestModel):
    item_number: int
    description: str | None = None
    damage_code: str | None = None
    quantity: Decimal | None = None


class AttributeOut(RestModel):
    name: str
    value: str | None


class NotificationOut(RestModel):
    notification_id: NotificationIdStr
    notification_type: NotificationType
    description: str
    priority: Priority | None = None
    plant: str
    functional_location: str | None = None
    reported_by: str | None = None
    required_start: date | None = None
    planned_cost: Decimal | None = None
    status: NotificationStatus
    created_on: date
    created_at: datetime
    items: list[ItemOut] = Field(default_factory=list)
    long_text: list[str] = Field(default_factory=list)
    attributes: list[AttributeOut] = Field(default_factory=list)


class MessageOut(RestModel):
    severity: MessageSeverity
    text: str


class NotificationCreated(RestModel):
    notification_id: NotificationIdStr
    messages: list[MessageOut] = Field(default_factory=list)


class NotificationSummaryOut(RestModel):
    notification_id: NotificationIdStr
    description: str
    status: NotificationStatus
    priority: Priority | None = None
    created_on: date


class NotificationSearchResult(RestModel):
    notifications: list[NotificationSummaryOut]
    truncated: bool
