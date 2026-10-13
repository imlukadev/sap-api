"""Rotas HTTP publicadas para o serviço de notas de manutenção."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from sap_gateway.auth import CurrentCredentials
from sap_gateway.errors import ErrorResponse
from sap_gateway.services.maintenance_notification import schemas
from sap_gateway.services.maintenance_notification.service import NotificationService

router = APIRouter(prefix="/api/v1/notifications", tags=["Notas de manutenção"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Credenciais ausentes ou rejeitadas."},
    403: {"model": ErrorResponse, "description": "Usuário sem autorização no sistema de destino."},
    422: {"model": ErrorResponse, "description": "Dados de entrada inválidos."},
    502: {
        "model": ErrorResponse,
        "description": "Falha ou resposta inválida do sistema de destino.",
    },
    503: {"model": ErrorResponse, "description": "Contrato (WSDL/XSD) do destino indisponível."},
    504: {"model": ErrorResponse, "description": "Timeout do sistema de destino."},
}


def get_notification_service(request: Request) -> NotificationService:
    service: NotificationService = request.app.state.notification_service
    return service


ServiceDep = Annotated[NotificationService, Depends(get_notification_service)]


@router.get(
    "",
    operation_id="search_notifications",
    summary="Pesquisar notas de manutenção",
    response_model=schemas.NotificationSearchResult,
    responses={200: {"description": "Resultado (possivelmente vazio)."}, **_ERRORS},
)
def search_notifications(
    query: Annotated[schemas.NotificationSearchQuery, Query()],
    credentials: CurrentCredentials,
    service: ServiceDep,
) -> schemas.NotificationSearchResult:
    """Executa ``NotificationSearch`` com as credenciais do chamador. Idempotente (com retry)."""
    return service.search(query, credentials)


@router.get(
    "/{notification_id}",
    operation_id="get_notification",
    summary="Consultar uma nota de manutenção",
    response_model=schemas.NotificationOut,
    responses={404: {"model": ErrorResponse, "description": "Nota inexistente."}, **_ERRORS},
)
def get_notification(
    notification_id: Annotated[str, Path(pattern=r"^[0-9]{12}$", examples=["000000000001"])],
    credentials: CurrentCredentials,
    service: ServiceDep,
) -> schemas.NotificationOut:
    """Executa ``NotificationRead`` (idempotente: com retry)."""
    return service.get(notification_id, credentials)


@router.post(
    "",
    operation_id="create_notification",
    summary="Criar uma nota de manutenção",
    status_code=201,
    response_model=schemas.NotificationCreated,
    responses={201: {"description": "Nota criada."}, **_ERRORS},
)
def create_notification(
    body: schemas.NotificationCreate,
    credentials: CurrentCredentials,
    service: ServiceDep,
    response: Response,
) -> schemas.NotificationCreated:
    """Executa ``NotificationCreate``. **Não idempotente**: nunca há retry automático após o envio
    da requisição (apenas falhas anteriores à conexão são repetidas)."""
    created = service.create(body, credentials)
    response.headers["Location"] = f"/api/v1/notifications/{created.notification_id}"
    return created
