from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helpers import USERS, Collected, build_collected, build_settings, build_soap
from sap_gateway.config import Settings
from sap_gateway.devtools.mock_sap import MockSapServer
from sap_gateway.main import create_app


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("APP_", "MOCK_SAP_")) or (
            name.startswith("OTEL_") and name != "OTEL_SEMCONV_STABILITY_OPT_IN"
        ):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def mock_sap_server() -> Iterator[MockSapServer]:
    server = MockSapServer(USERS, slow_seconds=2.0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_sap(mock_sap_server: MockSapServer) -> Iterator[MockSapServer]:
    mock_sap_server.mock.reset()
    yield mock_sap_server
    mock_sap_server.mock.reset()


@pytest.fixture
def basic_auth() -> Callable[[str], tuple[str, str]]:
    return lambda user: (user, USERS[user])


class AppBundle:
    def __init__(self, app: FastAPI, collected: Collected, sleeps: list[float]) -> None:
        self.app = app
        self.collected = collected
        self.sleeps = sleeps


@pytest.fixture
def make_app(mock_sap: MockSapServer) -> Iterator[Callable[..., AppBundle]]:
    created: list[Collected] = []

    def factory(settings: Settings | None = None, **service_overrides: object) -> AppBundle:
        settings = settings or build_settings(mock_sap.endpoint, **service_overrides)
        collected = build_collected(settings)
        created.append(collected)
        sleeps: list[float] = []
        soap = build_soap(settings, collected, sleeps=sleeps)
        app = create_app(settings, telemetry=collected.telemetry, soap=soap)
        return AppBundle(app, collected, sleeps)

    yield factory
    for collected in created:
        collected.telemetry.shutdown()


@pytest.fixture
def bundle(make_app: Callable[..., AppBundle]) -> AppBundle:
    return make_app()


@pytest.fixture
def client(bundle: AppBundle) -> Iterator[TestClient]:
    with TestClient(bundle.app) as test_client:
        yield test_client
