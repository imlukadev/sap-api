from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["Saúde"])


class LiveStatus(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyStatus(BaseModel):
    status: Literal["ready", "unavailable"]
    contracts_loaded: dict[str, bool]


@router.get("/live", response_model=LiveStatus, summary="Liveness: o processo está de pé")
def live() -> LiveStatus:
    return LiveStatus()


@router.get(
    "/ready",
    response_model=ReadyStatus,
    responses={503: {"model": ReadyStatus}},
    summary="Readiness: a aplicação aceita requisições",
)
def ready(request: Request, response: Response) -> ReadyStatus:
    """Não consulta o SAP (evita propagar indisponibilidade externa para o balanceador).

    ``contracts_loaded`` é informativo: o WSDL é carregado sob demanda na primeira chamada.
    """
    state = request.app.state
    is_ready = state.telemetry.is_ready
    if not is_ready:
        response.status_code = 503
    loaded = {key: state.gateway.wsdl_store.is_loaded(key) for key in state.service_keys}
    return ReadyStatus(status="ready" if is_ready else "unavailable", contracts_loaded=loaded)
