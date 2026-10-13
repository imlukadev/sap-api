"""Orquestração do caso de uso: REST → contrato SOAP → gateway → REST, e tradução de faults."""

from __future__ import annotations

from sap_gateway.auth import BasicCredentials
from sap_gateway.errors import (
    AuthorizationError,
    BusinessRuleError,
    NotFoundError,
    SoapFaultError,
)
from sap_gateway.services.maintenance_notification import contracts as soap
from sap_gateway.services.maintenance_notification import mappers, schemas
from sap_gateway.soap.contracts import SoapModel, SoapOperation
from sap_gateway.soap.gateway import SoapGateway

FAULT_NOT_FOUND = "NOTIFICATION_NOT_FOUND"
FAULT_NOT_AUTHORIZED = "NOT_AUTHORIZED"
FAULT_INVALID_INPUT = "INVALID_INPUT"

type TranslatedFault = SoapFaultError | NotFoundError | AuthorizationError | BusinessRuleError


def translate_fault(error: SoapFaultError) -> TranslatedFault:
    """Converte faults *de domínio* em erros HTTP específicos; os demais seguem como 502."""
    detail = error.detail
    if not isinstance(detail, soap.NotificationFault):
        return error
    if detail.code == FAULT_NOT_FOUND:
        return NotFoundError(public_message="Nota de manutenção não encontrada.")
    if detail.code == FAULT_NOT_AUTHORIZED:
        return AuthorizationError()
    if detail.code == FAULT_INVALID_INPUT:
        return BusinessRuleError()
    return error


class NotificationService:
    def __init__(self, gateway: SoapGateway) -> None:
        self._gateway = gateway

    def get(self, notification_id: str, credentials: BasicCredentials) -> schemas.NotificationOut:
        request = soap.NotificationReadRequest(notification_id=notification_id)
        response = self._call(soap.NOTIFICATION_READ, request, credentials)
        return mappers.notification_from_soap(response.notification)

    def search(
        self, query: schemas.NotificationSearchQuery, credentials: BasicCredentials
    ) -> schemas.NotificationSearchResult:
        request = mappers.search_query_to_soap(query)
        return mappers.search_result_from_soap(
            self._call(soap.NOTIFICATION_SEARCH, request, credentials)
        )

    def create(
        self, body: schemas.NotificationCreate, credentials: BasicCredentials
    ) -> schemas.NotificationCreated:
        request = mappers.create_request_to_soap(body)
        return mappers.created_from_soap(self._call(soap.NOTIFICATION_CREATE, request, credentials))

    def _call[Req: SoapModel, Resp: SoapModel](
        self,
        operation: SoapOperation[Req, Resp],
        request: Req,
        credentials: BasicCredentials,
    ) -> Resp:
        try:
            return self._gateway.call(operation, request, credentials)
        except SoapFaultError as exc:
            translated = translate_fault(exc)
            if translated is exc:
                raise
            raise translated from None
