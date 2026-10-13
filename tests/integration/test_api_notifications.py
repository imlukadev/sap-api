"""Rotas FastAPI → gateway SOAP real (Zeep + requests) → mock SAP HTTP."""

import pytest
from lxml import etree

from sap_gateway.devtools import mock_sap as m

NS = {"n": m.NS, "xsi": m.XSI}
BASE = "/api/v1/notifications"


def test_read_turns_empty_and_nil_elements_into_empty_string_and_null(client, basic_auth):
    response = client.get(f"{BASE}/{m.ID_SAMPLE_TRISTATE}", auth=basic_auth("alice"))
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == ""
    assert body["reported_by"] is None and body["planned_cost"] is None
    assert body["functional_location"] is None and body["priority"] is None
    assert body["items"] == [
        {"item_number": 1, "description": None, "damage_code": None, "quantity": None}
    ]
    assert body["long_text"] == [] and body["attributes"] == []


def test_read_maps_nested_lists_enums_dates_and_decimals(client, basic_auth):
    body = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice")).json()
    assert body["notification_type"] == "malfunction_report"
    assert body["priority"] == "high" and body["status"] == "in_process"
    assert body["planned_cost"] == "1234.50"
    assert body["created_at"].startswith("2026-03-01T10:20:30")
    assert [i["item_number"] for i in body["items"]] == [1, 2]
    assert body["items"][0]["quantity"] == "2.5"
    assert body["long_text"] == ["Linha 1", "Linha 2"]
    assert body["attributes"] == [{"name": "area", "value": "norte"}]


def test_create_sends_expected_soap_and_returns_location(client, mock_sap, basic_auth):
    response = client.post(
        BASE,
        auth=basic_auth("alice"),
        json={
            "notification_type": "maintenance_request",
            "description": "Nova nota",
            "plant": "1000",
            "reported_by": None,
            "functional_location": "",
            "items": [{"item_number": 1, "damage_code": None, "quantity": "1.5"}],
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert response.headers["location"] == f"{BASE}/{created['notification_id']}"
    assert created["messages"] == [{"severity": "success", "text": "Nota criada com sucesso"}]

    sent = mock_sap.mock.recorded()[-1]
    assert sent.operation == "NotificationCreate"
    assert sent.soap_action and sent.soap_action.strip('"').endswith(":NotificationCreate")
    root = etree.fromstring(sent.body).find(".//n:NotificationCreateRequest", NS)
    header = root.find("n:Header", NS)
    assert header.findtext("n:NotificationType", namespaces=NS) == "M1"
    assert header.find("n:ReportedBy", NS) is None, "None é omitido, não enviado como xsi:nil"
    assert header.find("n:FunctionalLocation", NS).text in (None, "")
    assert header.find("n:Priority", NS) is None
    item = root.find("n:Items/n:Item", NS)
    assert item.find("n:DamageCode", NS) is None
    assert item.findtext("n:Quantity", namespaces=NS) == "1.5"

    fetched = client.get(
        created["notification_id"].join([f"{BASE}/", ""]), auth=basic_auth("alice")
    )
    assert fetched.status_code == 200 and fetched.json()["description"] == "Nova nota"


def test_search_filters_and_empty_collections(client, mock_sap, basic_auth):
    auth = basic_auth("alice")
    result = client.get(BASE, params={"status": "in_process", "plant": "1000"}, auth=auth).json()
    assert [n["notification_id"] for n in result["notifications"]] == [m.ID_SAMPLE_FULL]
    sent = etree.fromstring(mock_sap.mock.recorded()[-1].body).find(
        ".//n:NotificationSearchRequest", NS
    )
    assert sent.findtext("n:Status", namespaces=NS) == "NOPR"
    assert sent.findtext("n:MaxResults", namespaces=NS) == "50"
    assert sent.find("n:CreatedFrom", NS) is None

    assert client.get(BASE, params={"plant": m.PLANT_EMPTY_COLLECTION}, auth=auth).json() == {
        "notifications": [],
        "truncated": False,
    }
    assert client.get(BASE, params={"plant": m.PLANT_NO_COLLECTION}, auth=auth).json() == {
        "notifications": [],
        "truncated": False,
    }
    truncated = client.get(BASE, params={"max_results": 1}, auth=auth).json()
    assert len(truncated["notifications"]) == 1 and truncated["truncated"] is True


@pytest.mark.parametrize(
    ("notification_id", "status", "code"),
    [
        (m.ID_NOT_FOUND, 404, "not_found"),
        (m.ID_NOT_AUTHORIZED, 403, "forbidden"),
        (m.ID_HTTP_500, 502, "soap_transport_error"),
        (m.ID_HTTP_503, 502, "soap_transport_error"),
        (m.ID_MALFORMED_XML, 502, "soap_response_error"),
        (m.ID_WRONG_ELEMENT, 502, "soap_response_error"),
        (m.ID_BAD_ENUM, 502, "soap_response_error"),
    ],
)
def test_upstream_failures_become_consistent_http_errors(
    client, basic_auth, notification_id, status, code
):
    response = client.get(f"{BASE}/{notification_id}", auth=basic_auth("alice"))
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert len(error["trace_id"]) == 32
    for leaked in ("faultstring", "internal detail", "boom", "<html", "Traceback", "127.0.0.1"):
        assert leaked not in response.text


def test_read_timeout_maps_to_504(make_app, basic_auth):
    from fastapi.testclient import TestClient

    bundle = make_app(read_timeout_seconds=0.3)
    with TestClient(bundle.app) as client:
        response = client.get(f"{BASE}/{m.ID_SLOW}", auth=basic_auth("alice"))
    assert response.status_code == 504 and response.json()["error"]["code"] == "soap_timeout"


def test_domain_fault_on_create_maps_to_422(client, basic_auth):
    response = client.post(
        BASE,
        auth=basic_auth("alice"),
        json={
            "notification_type": "activity_report",
            "description": "FAIL_INVALID",
            "plant": "1000",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "business_rule_violation"


def test_wrong_password_is_reported_as_authentication_failure(client):
    response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=("alice", "wrong"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert response.headers["www-authenticate"].startswith("Basic")
    assert "Logon failed" not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic !!!not-base64"},
        {"Authorization": "Bearer abc"},
        {"Authorization": "Basic"},
    ],
)
def test_missing_or_malformed_basic_auth_never_reaches_sap(client, mock_sap, headers):
    response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert mock_sap.mock.recorded() == []


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (f"{BASE}/abc", {}),
        (f"{BASE}/00000000000", {}),
        (BASE, {"plant": "10"}),
        (BASE, {"max_results": 0}),
        (BASE, {"bogus": 1}),
    ],
)
def test_invalid_input_is_rejected_before_calling_sap(client, mock_sap, basic_auth, path, params):
    response = client.get(path, params=params, auth=basic_auth("alice"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert mock_sap.mock.recorded() == []


def test_openapi_documents_security_models_and_error_responses(client):
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["BasicAuth"]["scheme"] == "basic"
    get_op = spec["paths"][f"{BASE}/{{notification_id}}"]["get"]
    assert get_op["security"] == [{"BasicAuth": []}]
    assert set(get_op["responses"]) >= {"200", "401", "403", "404", "422", "502", "503", "504"}
    assert get_op["responses"]["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
    assert spec["paths"][BASE]["post"]["responses"]["201"]
    assert not any("soap" in p.lower() and "{" in p and "operation" in p for p in spec["paths"])


def test_no_generic_soap_passthrough_endpoint_exists(client, basic_auth):
    for path in ("/soap", "/api/v1/soap/maintenance_notification/NotificationRead", "/api/v1/call"):
        assert client.post(path, auth=basic_auth("alice"), json={}).status_code in (404, 405)


def test_health_endpoints(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
