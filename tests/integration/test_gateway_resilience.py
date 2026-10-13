"""Timeouts, falhas de conexão e política de retry (idempotência) contra o mock SAP real."""

import socket

import pytest
from fastapi.testclient import TestClient

from sap_gateway.devtools import mock_sap as m

BASE = "/api/v1/notifications"
CREATE = {"notification_type": "activity_report", "plant": "1000"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def operations(mock_sap) -> list[str]:
    return [r.operation for r in mock_sap.mock.recorded()]


def test_idempotent_read_is_retried_on_http_503(client, bundle, mock_sap, basic_auth):
    response = client.get(f"{BASE}/{m.ID_HTTP_503}", auth=basic_auth("alice"))
    assert response.status_code == 502
    assert operations(mock_sap) == ["NotificationRead"] * 3
    assert len(bundle.sleeps) == 2


def test_idempotent_read_timeout_is_retried(make_app, mock_sap, basic_auth):
    bundle = make_app(read_timeout_seconds=0.2)
    with TestClient(bundle.app) as client:
        response = client.get(f"{BASE}/{m.ID_SLOW}", auth=basic_auth("alice"))
    assert response.status_code == 504
    assert operations(mock_sap) == ["NotificationRead"] * 3
    points = bundle.collected.metric_points()
    assert sum(p.value for p in points["soap.client.timeouts"]) == 3
    assert sum(p.value for p in points["soap.client.retries"]) == 2


def test_non_idempotent_create_is_not_retried_after_timeout(make_app, mock_sap, basic_auth):
    bundle = make_app(read_timeout_seconds=0.2)
    with TestClient(bundle.app) as client:
        response = client.post(
            BASE, auth=basic_auth("alice"), json={**CREATE, "description": "FAIL_SLOW"}
        )
    assert response.status_code == 504
    assert operations(mock_sap) == ["NotificationCreate"], (
        "criação duplicada: retry indevido após timeout"
    )
    assert bundle.sleeps == []


def test_non_idempotent_create_is_not_retried_on_http_503(client, mock_sap, basic_auth):
    response = client.post(
        BASE, auth=basic_auth("alice"), json={**CREATE, "description": "FAIL_HTTP_503"}
    )
    assert response.status_code == 502
    assert operations(mock_sap) == ["NotificationCreate"]


def test_soap_faults_and_auth_errors_are_never_retried(client, mock_sap, basic_auth):
    client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=("alice", "wrong"))
    client.get(f"{BASE}/{m.ID_MALFORMED_XML}", auth=basic_auth("alice"))
    assert operations(mock_sap) == ["NotificationRead", "NotificationRead"]


def test_connection_refused_is_retried_even_for_create_because_nothing_was_sent(
    make_app, basic_auth
):
    settings_bundle = make_app(endpoint_url=f"http://127.0.0.1:{free_port()}/soap")
    from helpers import build_settings

    del settings_bundle
    bundle = make_app(build_settings(f"http://127.0.0.1:{free_port()}/soap"))
    with TestClient(bundle.app) as client:
        read = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
        assert read.status_code == 502 and read.json()["error"]["code"] == "soap_connection_error"
        assert len(bundle.sleeps) == 2
        bundle.sleeps.clear()
        create = client.post(BASE, auth=basic_auth("alice"), json={**CREATE, "description": "x"})
        assert (
            create.status_code == 502 and create.json()["error"]["code"] == "soap_connection_error"
        )
        assert len(bundle.sleeps) == 2
    points = bundle.collected.metric_points()
    errors = {p.attributes.get("error.type") for p in points["soap.client.calls"]}
    assert errors == {"connection"}


def test_wsdl_unavailable_returns_503_and_recovers(make_app, mock_sap, basic_auth, tmp_path):
    from helpers import build_settings

    missing = tmp_path / "later.wsdl"
    bundle = make_app(build_settings(mock_sap.endpoint, wsdl=str(missing)))
    with TestClient(bundle.app) as client:
        first = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
        assert (
            first.status_code == 503
            and first.json()["error"]["code"] == "soap_contract_unavailable"
        )
        assert mock_sap.mock.recorded() == []
        ready = client.get("/health/ready").json()
        assert ready["contracts_loaded"] == {"maintenance_notification": False}
        for name in ("maintenance_notification.wsdl", "maintenance_notification.xsd"):
            (tmp_path / name).write_bytes((m.WSDL_DIR / name).read_bytes())
        missing.write_bytes((m.WSDL_DIR / "maintenance_notification.wsdl").read_bytes())
        second = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
        assert second.status_code == 200
        assert client.get("/health/ready").json()["contracts_loaded"] == {
            "maintenance_notification": True
        }


def test_remote_wsdl_and_xsd_load_with_technical_credentials_only_from_allowed_host(
    make_app, mock_sap, basic_auth
):
    from helpers import USERS, build_settings

    wsdl_url = f"http://127.0.0.1:{mock_sap.port}{m.SOAP_PATH}?wsdl"
    settings = build_settings(
        mock_sap.endpoint, wsdl=wsdl_url, wsdl_username="alice", wsdl_password=USERS["alice"]
    )
    bundle = make_app(settings)
    with TestClient(bundle.app) as client:
        response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("bob"))
    assert response.status_code == 200
    calls = mock_sap.mock.recorded()
    assert [c.username for c in calls] == ["bob"], (
        "a chamada de negócio deve usar a credencial do usuário"
    )


def test_insecure_endpoint_is_refused_outside_local_and_test(make_app, mock_sap, basic_auth):
    from helpers import build_settings

    bundle = make_app(build_settings(mock_sap.endpoint, environment="production"))
    with TestClient(bundle.app) as client:
        response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    assert response.status_code == 503
    assert mock_sap.mock.recorded() == [], "credenciais não podem sair em claro (http) em produção"


@pytest.mark.parametrize("attempts", [1, 2])
def test_retry_limit_is_configurable(make_app, mock_sap, basic_auth, attempts):
    from helpers import build_settings

    bundle = make_app(build_settings(mock_sap.endpoint, retry={"max_attempts": attempts}))
    with TestClient(bundle.app) as client:
        client.get(f"{BASE}/{m.ID_HTTP_503}", auth=basic_auth("alice"))
    assert len(operations(mock_sap)) == attempts
