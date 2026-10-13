"""Casos de uso: REST → modelo SAP → ``SoapService`` → REST, e tradução de faults de domínio."""

from __future__ import annotations

from collections.abc import Callable

from sap_gateway.auth import BasicCredentials
from sap_gateway.errors import AuthorizationError, BusinessRuleError, NotFoundError
from sap_gateway.services.maintenance_notification import contracts as sap
from sap_gateway.services.maintenance_notification import schemas as rest
from sap_gateway.soap.errors import SoapFaultError, SoapResponseError
from sap_gateway.soap.service import SoapService

NOTIFICATION_TYPE_TO_SAP = {
    rest.NotificationType.MAINTENANCE_REQUEST: "M1",
    rest.NotificationType.MALFUNCTION_REPORT: "M2",
    rest.NotificationType.ACTIVITY_REPORT: "M3",
}
PRIORITY_TO_SAP = {
    rest.Priority.VERY_HIGH: "1",
    rest.Priority.HIGH: "2",
    rest.Priority.MEDIUM: "3",
    rest.Priority.LOW: "4",
}
STATUS_TO_SAP = {
    rest.NotificationStatus.OUTSTANDING: "OSNO",
    rest.NotificationStatus.IN_PROCESS: "NOPR",
    rest.NotificationStatus.COMPLETED: "NOCO",
}
SEVERITY_FROM_SAP = {
    "S": rest.MessageSeverity.SUCCESS,
    "I": rest.MessageSeverity.INFO,
    "W": rest.MessageSeverity.WARNING,
    "E": rest.MessageSeverity.ERROR,
}
NOTIFICATION_TYPE_FROM_SAP = {v: k for k, v in NOTIFICATION_TYPE_TO_SAP.items()}
PRIORITY_FROM_SAP = {v: k for k, v in PRIORITY_TO_SAP.items()}
STATUS_FROM_SAP = {v: k for k, v in STATUS_TO_SAP.items()}


def _code[K, V](table: dict[K, V], key: K) -> V:
    """Código SAP desconhecido = resposta fora do contrato (erro explícito, nunca KeyError)."""
    try:
        return table[key]
    except KeyError:
        raise SoapResponseError() from None


def _translate_fault(error: SoapFaultError) -> Exception:
    """Faults de domínio viram erros HTTP específicos; os demais seguem como 502."""
    match error.find("Code"):
        case "NOTIFICATION_NOT_FOUND":
            return NotFoundError(public_message="Nota de manutenção não encontrada.")
        case "NOT_AUTHORIZED":
            return AuthorizationError()
        case "INVALID_INPUT":
            return BusinessRuleError()
        case _:
            return error


class NotificationService:
    def __init__(self, soap: SoapService) -> None:
        self._soap = soap

    def get(self, notification_id: str, credentials: BasicCredentials) -> rest.NotificationOut:
        data = self._call(
            lambda: self._soap.call(
                "NotificationRead",
                credentials,
                sap.ReadRequest(notification_id=notification_id),
                response_model=sap.NotificationData,
                idempotent=True,
            )
        )
        header = data.header
        return rest.NotificationOut(
            notification_id=data.notification_id,
            notification_type=_code(NOTIFICATION_TYPE_FROM_SAP, header.notification_type),
            description=header.description,
            priority=_code(PRIORITY_FROM_SAP, header.priority) if header.priority else None,
            plant=header.plant,
            functional_location=header.functional_location,
            reported_by=header.reported_by,
            required_start=header.required_start,
            planned_cost=header.planned_cost,
            status=_code(STATUS_FROM_SAP, data.status),
            created_on=data.created_on,
            created_at=data.created_at,
            items=[
                rest.ItemOut(**item.model_dump())
                for item in (data.items.item if data.items else [])
            ],
            long_text=data.long_text.text_line if data.long_text else [],
            attributes=[
                rest.AttributeOut(**a.model_dump())
                for a in (data.attributes.attribute if data.attributes else [])
            ],
        )

    def search(
        self, query: rest.NotificationSearchQuery, credentials: BasicCredentials
    ) -> rest.NotificationSearchResult:
        response = self._call(
            lambda: self._soap.call(
                "NotificationSearch",
                credentials,
                sap.SearchRequest(
                    plant=query.plant,
                    status=STATUS_TO_SAP[query.status] if query.status else None,
                    created_from=query.created_from,
                    created_to=query.created_to,
                    max_results=query.max_results,
                ),
                response_model=sap.SearchResponse,
                idempotent=True,
            )
        )
        found = response.notifications.notification if response.notifications else []
        return rest.NotificationSearchResult(
            notifications=[
                rest.NotificationSummaryOut(
                    notification_id=s.notification_id,
                    description=s.description,
                    status=_code(STATUS_FROM_SAP, s.status),
                    priority=_code(PRIORITY_FROM_SAP, s.priority) if s.priority else None,
                    created_on=s.created_on,
                )
                for s in found
            ],
            truncated=response.truncated,
        )

    def create(
        self, body: rest.NotificationCreate, credentials: BasicCredentials
    ) -> rest.NotificationCreated:
        header = sap.Header(
            notification_type=NOTIFICATION_TYPE_TO_SAP[body.notification_type],
            description=body.description,
            plant=body.plant,
            priority=PRIORITY_TO_SAP[body.priority] if body.priority else None,
            functional_location=body.functional_location,
            reported_by=body.reported_by,
            required_start=body.required_start,
            planned_cost=body.planned_cost,
        )
        request = sap.CreateRequest(
            header=header,
            items=sap.ItemList(item=[sap.Item(**i.model_dump()) for i in body.items])
            if body.items
            else None,
            long_text=sap.LongText(text_line=body.long_text) if body.long_text else None,
            attributes=sap.AttributeList(
                attribute=[sap.Attribute(**a.model_dump()) for a in body.attributes]
            )
            if body.attributes
            else None,
        )
        response = self._call(
            lambda: self._soap.call(
                "NotificationCreate",  # criação: não idempotente → sem retry após o envio
                credentials,
                request,
                response_model=sap.CreateResponse,
            )
        )
        messages = response.messages.message if response.messages else []
        return rest.NotificationCreated(
            notification_id=response.notification_id,
            messages=[
                rest.MessageOut(severity=_code(SEVERITY_FROM_SAP, m.severity), text=m.text)
                for m in messages
            ],
        )

    @staticmethod
    def _call[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except SoapFaultError as exc:
            translated = _translate_fault(exc)
            if translated is exc:
                raise
            raise translated from None
