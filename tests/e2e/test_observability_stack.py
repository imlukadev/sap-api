"""Teste e2e: API → OTLP → Alloy → Tempo / Loki / Prometheus, consultando as APIs dos backends.

Pré-requisito (não roda no CI padrão):

    docker compose up -d
    RUN_E2E=1 uv run pytest tests/e2e -v

A API roda dentro do próprio teste (TestClient) exportando por OTLP/gRPC para o Alloy publicado em
``localhost:4317``. Um usuário aleatório torna a busca por ``username``/``enduser.id`` inequívoca.
Variáveis opcionais: E2E_OTLP_ENDPOINT, E2E_TEMPO_URL, E2E_LOKI_URL, E2E_PROMETHEUS_URL.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import build_settings
from sap_gateway.config import TelemetrySettings
from sap_gateway.devtools import mock_sap as m
from sap_gateway.devtools.mock_sap import MockSapServer
from sap_gateway.main import create_app
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.soap.gateway import SoapGateway

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("RUN_E2E") != "1", reason="defina RUN_E2E=1 com a stack no ar"
    ),
]

OTLP = os.environ.get("E2E_OTLP_ENDPOINT", "http://localhost:4317")
TEMPO = os.environ.get("E2E_TEMPO_URL", "http://localhost:3200")
LOKI = os.environ.get("E2E_LOKI_URL", "http://localhost:3100")
PROMETHEUS = os.environ.get("E2E_PROMETHEUS_URL", "http://localhost:9090")


def get_json(url: str, **params: str) -> Any:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    with urllib.request.urlopen(f"{url}{query}", timeout=10) as response:
        return json.load(response)


def eventually[T](probe: Callable[[], T | None], *, what: str, timeout: float = 90.0) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - backend ainda subindo/indexando
            last_error = exc
        time.sleep(2)
    raise AssertionError(f"timeout esperando {what} (último erro: {last_error})")


@pytest.fixture(scope="module")
def scenario():
    username = f"e2e-{uuid.uuid4().hex[:8]}"
    password = f"pw-{uuid.uuid4().hex}"
    sap = MockSapServer({username: password}, slow_seconds=2)
    sap.start()
    telemetry_settings = TelemetrySettings(
        _env_file=None,
        traces_enabled=True,
        metrics_enabled=True,
        logs_enabled=True,
        otlp_endpoint=OTLP,
        otlp_protocol="grpc",
        batch_schedule_delay_ms=500,
        metric_export_interval_ms=2000,
    )
    settings = build_settings(sap.endpoint, environment="local", telemetry=telemetry_settings)
    telemetry = Telemetry(settings)
    app = create_app(settings, telemetry=telemetry, gateway=SoapGateway(settings, telemetry))
    with TestClient(app) as client:
        ok = client.get(f"/api/v1/notifications/{m.ID_SAMPLE_FULL}", auth=(username, password))
        missing = client.get(f"/api/v1/notifications/{m.ID_NOT_FOUND}", auth=(username, password))
        assert (ok.status_code, missing.status_code) == (200, 404)
        trace_id = missing.json()["error"]["trace_id"]
    sap.stop()
    return {"username": username, "password": password, "error_trace_id": trace_id}


def test_traces_reach_tempo_with_user_and_soap_child_span(scenario):
    username = scenario["username"]

    def probe():
        found = get_json(
            f"{TEMPO}/api/search", q=f'{{ span.enduser.id = "{username}" }}', limit="20"
        )
        return found.get("traces") or None

    traces = eventually(probe, what="traces do usuário no Tempo")
    trace = get_json(f"{TEMPO}/api/traces/{scenario['error_trace_id']}")
    spans = [
        span
        for batch in trace.get("batches", trace.get("resourceSpans", []))
        for scope in batch["scopeSpans"]
        for span in scope["spans"]
    ]
    names = {span["name"] for span in spans}
    assert "GET /api/v1/notifications/{notification_id}" in names
    assert "SOAP maintenance_notification/NotificationRead" in names
    assert any(trace["rootServiceName"] == "sap-gateway" for trace in traces)
    assert scenario["password"] not in json.dumps(trace)


def test_logs_reach_loki_with_user_as_structured_metadata_not_label(scenario):
    username = scenario["username"]
    labels = get_json(f"{LOKI}/loki/api/v1/labels")["data"]
    assert "username" not in labels and "trace_id" not in labels, (
        "atributos de alta cardinalidade viraram labels"
    )

    def probe():
        found = get_json(
            f"{LOKI}/loki/api/v1/query_range",
            query=f'{{service_name="sap-gateway"}} | username="{username}" |= "http request completed"',
            limit="10",
        )
        return found["data"]["result"] or None

    streams = eventually(probe, what="logs do usuário no Loki")
    entries = [stream["stream"] for stream in streams]
    assert {entry["http_response_status_code"] for entry in entries} == {"200", "404"}
    assert all(
        entry["http_route"] == "/api/v1/notifications/{notification_id}" for entry in entries
    )
    assert scenario["error_trace_id"] in {entry["trace_id"] for entry in entries}, (
        "log sem trace_id nativo"
    )
    assert all(len(entry["span_id"]) == 16 for entry in entries)
    assert scenario["password"] not in json.dumps(streams)


def test_metrics_reach_prometheus_without_user_dimension(scenario):
    def probe():
        result = get_json(
            f"{PROMETHEUS}/api/v1/query",
            query='soap_client_calls_total{soap_operation="NotificationRead"}',
        )
        return result["data"]["result"] or None

    series = eventually(probe, what="métricas SOAP no Prometheus")
    assert all(item["metric"]["soap_service"] == "maintenance_notification" for item in series)
    http = get_json(
        f"{PROMETHEUS}/api/v1/query", query="http_server_request_duration_seconds_count"
    )["data"]["result"]
    assert http
    everything = json.dumps(series + http)
    assert scenario["username"] not in everything
    for item in series + http:
        assert not {"username", "enduser_id", "trace_id", "notification_id"} & set(item["metric"])


def test_grafana_datasources_support_log_to_trace_correlation():
    grafana = os.environ.get("E2E_GRAFANA_URL", "http://localhost:3000")
    token = os.environ.get("E2E_GRAFANA_AUTH", "admin:admin")
    import base64

    request = urllib.request.Request(
        f"{grafana}/api/datasources",
        headers={"Authorization": "Basic " + base64.b64encode(token.encode()).decode()},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        datasources = {ds["uid"]: ds for ds in json.load(response)}
    loki = datasources["loki"]["jsonData"]["derivedFields"][0]
    assert loki["datasourceUid"] == "tempo" and loki["matcherRegex"] == "trace_id"
    assert datasources["tempo"]["jsonData"]["tracesToLogsV2"]["datasourceUid"] == "loki"
