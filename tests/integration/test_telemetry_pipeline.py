"""Traces, métricas e logs realmente emitidos pelos pipelines configurados (exporters em memória)."""

import logging

import pytest
from opentelemetry.trace import SpanKind

from helpers import USERS
from sap_gateway.config import Settings
from sap_gateway.devtools import mock_sap as m
from sap_gateway.observability.metrics import ALLOWED_SOAP_ATTRIBUTES

BASE = "/api/v1/notifications"
OPERATION = "NotificationRead"
SERVICE = "maintenance_notification"


def spans_by_name(collected):
    out = {}
    for span in collected.spans.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


def ancestors(span, by_id):
    while span.parent is not None and span.parent.span_id in by_id:
        span = by_id[span.parent.span_id]
        yield span


def test_trace_follows_request_from_http_entry_to_soap_http_call(client, bundle, basic_auth):
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    spans = bundle.collected.spans.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    server = next(s for s in spans if s.kind is SpanKind.SERVER)
    soap = next(s for s in spans if s.name == f"SOAP {SERVICE}/{OPERATION}")
    attempt = next(s for s in spans if s.name == "soap.attempt")
    http_client = next(s for s in spans if s.kind is SpanKind.CLIENT)

    assert {s.context.trace_id for s in spans} == {server.context.trace_id}
    assert server.name == "GET /api/v1/notifications/{notification_id}"
    assert server.attributes["http.route"] == "/api/v1/notifications/{notification_id}"
    assert server.attributes["http.request.method"] == "GET"
    assert server.attributes["http.response.status_code"] == 200
    assert soap.parent.span_id == server.context.span_id
    assert attempt.parent.span_id == soap.context.span_id
    assert http_client.parent.span_id == attempt.context.span_id
    assert server in list(ancestors(http_client, by_id))
    assert (
        soap.attributes["soap.service"] == SERVICE
        and soap.attributes["soap.operation"] == OPERATION
    )
    assert soap.attributes["soap.attempts"] == 1 and soap.attributes["soap.outcome"] == "success"
    assert http_client.attributes["http.response.status_code"] == 200


def test_authenticated_user_is_recorded_on_traces_and_logs_but_not_metrics(
    client, bundle, basic_auth
):
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("bob"))
    collected = bundle.collected
    server_users = [
        s.attributes["enduser.id"]
        for s in collected.spans.get_finished_spans()
        if s.kind is SpanKind.SERVER
    ]
    soap_users = [
        s.attributes["enduser.id"]
        for s in collected.spans.get_finished_spans()
        if s.name.startswith("SOAP ")
    ]
    assert server_users == ["alice", "bob"] and soap_users == ["alice", "bob"]
    exported = [
        r.log_record
        for r in collected.logs.get_finished_logs()
        if r.log_record.attributes.get("username")
    ]
    assert {r.attributes["username"] for r in exported} == {"alice", "bob"}
    assert {
        json_log["username"] for json_log in collected.json_logs() if "username" in json_log
    } == {"alice", "bob"}
    for points in collected.metric_points().values():
        for point in points:
            rendered = f"{dict(point.attributes)}"
            assert "alice" not in rendered and "bob" not in rendered
            assert "enduser" not in rendered and "username" not in rendered


def test_json_logs_have_the_documented_fields(client, bundle, basic_auth):
    client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("alice"))
    lines = bundle.collected.json_logs()
    access = next(line for line in lines if line["message"] == "http request completed")
    for key in (
        "timestamp",
        "severity",
        "message",
        "service.name",
        "deployment.environment",
        "username",
        "trace_id",
        "span_id",
        "http.route",
        "http.request.method",
        "http.response.status_code",
        "duration_ms",
    ):
        assert key in access, key
    assert access["service.name"] == "sap-gateway" and access["deployment.environment"] == "test"
    assert access["http.response.status_code"] == 404 and access["severity"] == "WARNING"
    assert access["error.type"] == "not_found"
    soap_line = next(line for line in lines if line["message"] == "soap call failed")
    assert soap_line["soap.service"] == SERVICE and soap_line["soap.operation"] == OPERATION
    assert soap_line["error.type"] == "soap_fault" and soap_line["username"] == "alice"
    assert soap_line["trace_id"] == access["trace_id"]
    assert len(access["trace_id"]) == 32 and len(access["span_id"]) == 16


def test_otel_log_pipeline_carries_native_trace_context_and_is_not_duplicated(
    client, bundle, basic_auth
):
    for _ in range(3):
        client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    collected = bundle.collected
    records = [r.log_record for r in collected.logs.get_finished_logs()]
    access = [r for r in records if r.body == "http request completed"]
    soap_logs = [r for r in records if r.body == "soap call completed"]
    assert len(access) == 3 and len(soap_logs) == 3, "logs exportados em duplicidade ou perdidos"
    server_traces = {
        s.context.trace_id
        for s in collected.spans.get_finished_spans()
        if s.kind is SpanKind.SERVER
    }
    assert {r.trace_id for r in access} == server_traces
    assert all(r.span_id != 0 for r in access)
    first = access[0].attributes
    assert (
        first["username"] == "alice"
        and first["http.route"] == "/api/v1/notifications/{notification_id}"
    )
    assert first["http.response.status_code"] == 200 and "duration_ms" in first
    assert not any(k.startswith("_sap") for k in first)
    stdout_access = [
        line for line in collected.json_logs() if line["message"] == "http request completed"
    ]
    assert len(stdout_access) == 3


def test_resource_attributes_are_shared_by_all_signals(bundle):
    attrs = bundle.collected.telemetry.resource.attributes
    assert attrs["service.name"] == "sap-gateway" and attrs["deployment.environment"] == "test"
    assert attrs["service.version"] and attrs["service.instance.id"]
    telemetry = bundle.collected.telemetry
    assert telemetry.tracer_provider.resource is telemetry.meter_provider._sdk_config.resource
    assert telemetry.logger_provider.resource is telemetry.resource


def test_metrics_are_low_cardinality_and_cover_http_and_soap(client, bundle, basic_auth):
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_SAMPLE_TRISTATE}", auth=basic_auth("bob"))
    client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_HTTP_503}", auth=basic_auth("alice"))
    points = bundle.collected.metric_points()

    for name in (
        "http.server.request.duration",
        "soap.client.calls",
        "soap.client.duration",
        "soap.client.retries",
        "app.health.status",
    ):
        assert name in points, name

    http = points["http.server.request.duration"]
    assert {p.attributes["http.route"] for p in http} == {"/api/v1/notifications/{notification_id}"}
    assert {p.attributes["http.response.status_code"] for p in http} == {200, 404, 502}
    assert sum(p.count for p in http) == 4

    for name in (
        "soap.client.calls",
        "soap.client.duration",
        "soap.client.retries",
        "soap.client.timeouts",
    ):
        for point in points.get(name, []):
            assert set(point.attributes) <= ALLOWED_SOAP_ATTRIBUTES, (name, dict(point.attributes))
            assert point.attributes["soap.service"] == SERVICE

    calls = {p.attributes.get("error.type"): p.value for p in points["soap.client.calls"]}
    assert calls == {None: 2, "soap_fault": 1, "http_status": 1}
    assert sum(p.value for p in points["soap.client.retries"]) == 2
    assert points["app.health.status"][0].value == 1

    every_value = " ".join(str(dict(p.attributes)) for pts in points.values() for p in pts)
    for forbidden in ("000000000", "alice", "bob", "notification_id=", "trace"):
        assert forbidden not in every_value


def test_retries_are_visible_in_spans_metrics_and_use_jittered_backoff(client, bundle, basic_auth):
    response = client.get(f"{BASE}/{m.ID_HTTP_503}", auth=basic_auth("alice"))
    assert response.status_code == 502
    assert bundle.sleeps == pytest.approx([0.2, 0.4])
    spans = bundle.collected.spans.get_finished_spans()
    soap = next(s for s in spans if s.name.startswith("SOAP "))
    assert [e.name for e in soap.events] == ["soap.retry", "soap.retry"]
    assert (
        soap.attributes["soap.attempts"] == 3 and soap.attributes["soap.outcome"] == "http_status"
    )
    assert soap.status.status_code.name == "ERROR"
    assert [s.attributes["soap.attempt"] for s in spans if s.name == "soap.attempt"] == [1, 2, 3]
    retry_logs = [r for r in bundle.collected.json_logs() if "retry" in r["message"]]
    assert retry_logs == [], "retries não devem gerar logs próprios"
    assert len([r for r in bundle.collected.json_logs() if r["message"] == "soap call failed"]) == 1


def test_spans_never_record_exception_messages_or_bodies(client, bundle, basic_auth):
    client.get(f"{BASE}/{m.ID_HTTP_500}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("alice"))
    for span in bundle.collected.spans.get_finished_spans():
        text = f"{span.attributes}{span.events}{span.status}"
        for leaked in ("boom", "internal detail", "faultstring", "<html", "Traceback"):
            assert leaked not in text


def test_probe_endpoints_are_not_traced_or_logged(client, bundle):
    client.get("/health/live")
    client.get("/health/ready")
    assert bundle.collected.spans.get_finished_spans() == ()
    assert [
        line for line in bundle.collected.json_logs() if line["message"] == "http request completed"
    ] == []


def test_unexpected_errors_return_500_and_log_sanitized_traceback(make_app, basic_auth):
    from fastapi.testclient import TestClient

    bundle = make_app()
    password = USERS["alice"]

    def explode(*_args, **_kwargs):
        raise RuntimeError(f"db failure password={password} Authorization: Basic QWxhZGRpbjpvcGVu")

    bundle.app.state.notification_service.get = explode
    with TestClient(bundle.app, raise_server_exceptions=False) as client:
        response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert password not in response.text
    stream = bundle.collected.stream.getvalue()
    assert "RuntimeError" in stream and "exception.stacktrace" in stream
    assert password not in stream and "QWxhZGRpbjpvcGVu" not in stream
    exported = " ".join(
        str(r.log_record.attributes) for r in bundle.collected.logs.get_finished_logs()
    )
    assert password not in exported and "QWxhZGRpbjpvcGVu" not in exported


def test_telemetry_initialisation_does_not_duplicate_handlers(make_app):
    root = logging.getLogger()

    def ours():
        return [h for h in root.handlers if getattr(h, "_sap_gateway_handler", False)]

    first = make_app()
    assert len(ours()) == 2
    second = make_app()
    assert len(ours()) == 2, "segundo init duplicou handlers"
    second.collected.telemetry.shutdown()
    assert ours() == []
    first.collected.telemetry.shutdown()
    first.collected.telemetry.shutdown()


def test_health_gauge_reflects_lifecycle(bundle):
    from fastapi.testclient import TestClient

    with TestClient(bundle.app) as client:
        assert client.get("/health/ready").status_code == 200
        assert bundle.collected.metric_points()["app.health.status"][0].value == 1
    assert bundle.collected.telemetry.is_ready is False
    assert isinstance(bundle.app.state.settings, Settings)
