"""Indisponibilidade do coletor não pode afetar o processamento nem travar o encerramento."""

import socket
import time

from fastapi.testclient import TestClient

from helpers import build_settings
from sap_gateway.config import TelemetrySettings
from sap_gateway.devtools import mock_sap as m
from sap_gateway.main import create_app
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.soap.gateway import SoapGateway

BASE = "/api/v1/notifications"


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_requests_succeed_and_shutdown_is_bounded_when_alloy_is_down(mock_sap, basic_auth):
    for protocol in ("grpc", "http/protobuf"):
        telemetry_settings = TelemetrySettings(
            _env_file=None,
            traces_enabled=True,
            metrics_enabled=True,
            logs_enabled=True,
            otlp_endpoint=f"http://127.0.0.1:{closed_port()}",
            otlp_protocol=protocol,
            otlp_timeout_ms=500,
            batch_schedule_delay_ms=100,
            metric_export_interval_ms=1000,
        )
        settings = build_settings(mock_sap.endpoint, telemetry=telemetry_settings)
        telemetry = Telemetry(settings)
        app = create_app(settings, telemetry=telemetry, gateway=SoapGateway(settings, telemetry))

        latencies = []
        with TestClient(app) as client:
            for _ in range(15):
                started = time.perf_counter()
                response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
                latencies.append(time.perf_counter() - started)
                assert response.status_code == 200
            time.sleep(1.2)
            assert (
                client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("alice")).status_code == 404
            )
            shutdown_started = time.perf_counter()
        shutdown_seconds = time.perf_counter() - shutdown_started

        assert max(latencies) < 1.0, (protocol, max(latencies))
        assert shutdown_seconds < 8, (protocol, shutdown_seconds)
