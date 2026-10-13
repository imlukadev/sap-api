"""Fábrica da aplicação FastAPI e ponto de entrada (``sap-gateway``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from sap_gateway.config import Settings, get_settings
from sap_gateway.errors import install_error_handlers
from sap_gateway.observability.middleware import RequestContextMiddleware
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.routes import health
from sap_gateway.services.maintenance_notification import contracts as notification_contracts
from sap_gateway.services.maintenance_notification.router import router as notification_router
from sap_gateway.services.maintenance_notification.service import NotificationService
from sap_gateway.soap.gateway import SoapGateway


def create_app(
    settings: Settings | None = None,
    *,
    telemetry: Telemetry | None = None,
    gateway: SoapGateway | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    telemetry = telemetry or Telemetry(settings)
    gateway = gateway or SoapGateway(settings, telemetry)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        telemetry.set_ready(True)
        try:
            yield
        finally:
            telemetry.shutdown()

    app = FastAPI(
        title="SAP Gateway",
        version=settings.effective_version(),
        summary="API REST sobre serviços SOAP corporativos",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.telemetry = telemetry
    app.state.gateway = gateway
    app.state.service_keys = [notification_contracts.SERVICE.key]
    app.state.notification_service = NotificationService(gateway)

    install_error_handlers(app, basic_realm=settings.basic_auth_realm)
    app.add_middleware(
        RequestContextMiddleware,
        quiet_paths=frozenset(settings.telemetry.http_excluded_urls),
    )
    app.include_router(health.router)
    app.include_router(notification_router)

    telemetry.instrument(app)
    return app


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )
