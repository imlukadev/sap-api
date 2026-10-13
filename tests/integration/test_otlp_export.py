"""Os três sinais saem do processo por OTLP (gRPC e HTTP/protobuf) — verificado no lado receptor."""

import pytest
from fastapi.testclient import TestClient

from helpers import build_settings, build_soap_for
from otlp_receiver import GrpcReceiver, HttpReceiver, attrs
from sap_gateway.config import TelemetrySettings
from sap_gateway.devtools import mock_sap as m
from sap_gateway.main import create_app
from sap_gateway.observability.telemetry import Telemetry

BASE = "/api/v1/notifications"


def run_scenario(mock_sap, receiver, protocol: str, basic_auth) -> None:
    telemetry_settings = TelemetrySettings(
        _env_file=None,
        traces_enabled=True,
        metrics_enabled=True,
        logs_enabled=True,
        otlp_endpoint=receiver.endpoint,
        otlp_protocol=protocol,
        otlp_timeout_ms=2000,
        batch_schedule_delay_ms=100,
        metric_export_interval_ms=1000,
    )
    settings = build_settings(mock_sap.endpoint, telemetry=telemetry_settings)
    telemetry = Telemetry(settings)
    app = create_app(settings, telemetry=telemetry, soap=build_soap_for(settings, telemetry))
    with TestClient(app) as client:
        assert client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice")).status_code == 200
        assert client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("bob")).status_code == 404


@pytest.fixture(params=["grpc", "http/protobuf"])
def exported(request, mock_sap, basic_auth):
    receiver = GrpcReceiver() if request.param == "grpc" else HttpReceiver()
    receiver.start()
    try:
        run_scenario(mock_sap, receiver, request.param, basic_auth)
        yield receiver.received
    finally:
        receiver.stop()


def test_traces_arrive_with_resource_and_user(exported):
    spans = exported.spans()
    assert spans, "nenhum span chegou ao receptor OTLP"
    names = {span.name for _, span in spans}
    assert "GET /api/v1/notifications/{notification_id}" in names
    assert f"SOAP {m.NS and 'maintenance_notification'}/NotificationRead" in names
    resource, _ = spans[0]
    assert resource["service.name"] == "sap-gateway"
    assert resource["deployment.environment"] == "test"
    assert resource["service.version"]
    soap_spans = [s for _, s in spans if s.name.startswith("SOAP ")]
    users = {attrs(s.attributes)["enduser.id"] for s in soap_spans}
    assert users == {"alice", "bob"}
    trace_ids = {s.trace_id for _, s in spans}
    assert len(trace_ids) == 2
    for _, span in spans:
        assert len(span.trace_id) == 16 and len(span.span_id) == 8
    secrets = "".join(str(s) for _, s in spans)
    assert "Alice-S3cret" not in secrets and "Bob-Pa55" not in secrets


def test_logs_arrive_with_native_trace_context_and_attributes(exported):
    records = exported.log_records()
    assert records, "nenhum log chegou ao receptor OTLP"
    access = [rec for _, rec in records if rec.body.string_value == "http request completed"]
    assert len(access) == 2
    span_traces = {s.trace_id for _, s in exported.spans()}
    for record in access:
        assert record.trace_id in span_traces, (
            "trace_id nativo do LogRecord deve apontar para um trace exportado"
        )
        assert record.span_id != b"\x00" * 8
        values = attrs(record.attributes)
        assert values["username"] in {"alice", "bob"}
        assert values["http.route"] == "/api/v1/notifications/{notification_id}"
        assert values["http.request.method"] == "GET"
        assert values["http.response.status_code"] in {200, 404}
        assert "duration_ms" in values
    resource, _ = records[0]
    assert (
        resource["service.name"] == "sap-gateway" and resource["deployment.environment"] == "test"
    )
    soap_logs = [
        attrs(rec.attributes) for _, rec in records if rec.body.string_value.startswith("soap call")
    ]
    assert {v["soap.operation"] for v in soap_logs} == {"NotificationRead"}
    everything = "".join(str(rec) for _, rec in records)
    assert "Alice-S3cret" not in everything and "Authorization" not in everything


def test_metrics_arrive_with_low_cardinality_attributes(exported):
    items = exported.metric_items()
    assert items, "nenhuma métrica chegou ao receptor OTLP"
    by_name = {metric.name: metric for _, metric in items}
    for name in (
        "http.server.request.duration",
        "soap.client.calls",
        "soap.client.duration",
        "app.health.status",
    ):
        assert name in by_name, name
    assert by_name["soap.client.duration"].unit == "s"
    points = by_name["soap.client.calls"].sum.data_points
    seen = {tuple(sorted(attrs(p.attributes).items())) for p in points}
    assert (
        ("soap.operation", "NotificationRead"),
        ("soap.service", "maintenance_notification"),
    ) in seen
    assert any(("error.type", "soap_fault") in key for key in seen)
    rendered = str([str(m) for _, m in items])
    assert "alice" not in rendered and "bob" not in rendered
    resource, _ = items[0]
    assert resource["service.name"] == "sap-gateway"


def test_http_exporter_uses_signal_specific_paths_and_grpc_uses_service_names():
    from sap_gateway.observability.telemetry import _build_exporters

    cfg = TelemetrySettings(
        _env_file=None,
        traces_enabled=True,
        otlp_protocol="http/protobuf",
        otlp_endpoint="http://alloy:4318",
    )
    span, metric, log = _build_exporters(cfg)
    assert span._endpoint == "http://alloy:4318/v1/traces"
    assert metric._endpoint == "http://alloy:4318/v1/metrics"
    assert log._endpoint == "http://alloy:4318/v1/logs"
    grpc_span, _, _ = _build_exporters(
        TelemetrySettings(_env_file=None, otlp_endpoint="http://alloy:4317")
    )
    assert type(grpc_span).__module__.endswith("grpc.trace_exporter")
