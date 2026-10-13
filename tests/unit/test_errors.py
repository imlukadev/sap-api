import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sap_gateway import errors
from sap_gateway.errors import install_error_handlers

CASES = [
    (errors.AuthenticationRequiredError(), 401, "authentication_required"),
    (errors.AuthenticationFailedError(), 401, "authentication_failed"),
    (errors.AuthorizationError(), 403, "forbidden"),
    (errors.NotFoundError(), 404, "not_found"),
    (errors.ConflictError(), 409, "conflict"),
    (errors.BusinessRuleError(), 422, "business_rule_violation"),
    (errors.SoapWsdlUnavailableError(), 503, "soap_contract_unavailable"),
    (errors.SoapConnectionError(request_sent=False), 502, "soap_connection_error"),
    (errors.SoapTimeoutError(request_sent=True), 504, "soap_timeout"),
    (errors.SoapTransportError(upstream_status=503), 502, "soap_transport_error"),
    (
        errors.SoapFaultError(fault_code="soap:Server", fault_message="internal secret detail"),
        502,
        "soap_fault",
    ),
    (errors.SoapMalformedResponseError(), 502, "soap_malformed_response"),
    (errors.ContractMappingError(), 502, "contract_mismatch"),
    (errors.SoapSerializationError(), 500, "soap_serialization_error"),
]


def make_client(exc: Exception) -> TestClient:
    app = FastAPI()
    install_error_handlers(app, basic_realm="r")

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("exc", "status", "code"), CASES, ids=[c[2] for c in CASES])
def test_app_errors_map_to_structured_http_responses(exc, status, code):
    response = make_client(exc).get("/boom")
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert set(body["error"]) <= {"code", "message", "trace_id", "details"}
    assert response.headers["content-type"].startswith("application/json")


def test_401_responses_carry_www_authenticate():
    response = make_client(errors.AuthenticationRequiredError(realm="sap")).get("/boom")
    assert response.headers["www-authenticate"] == 'Basic realm="sap"'


def test_soap_fault_content_is_never_exposed():
    response = make_client(
        errors.SoapFaultError(fault_code="soap:Server", fault_message="internal secret detail")
    ).get("/boom")
    assert "internal secret detail" not in response.text
    assert "soap:Server" not in response.text


def test_unexpected_exception_returns_generic_500_without_traceback():
    response = make_client(RuntimeError("db password is hunter2")).get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text and "Traceback" not in response.text


def test_validation_errors_do_not_echo_input_values():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/n/{value}")
    def read(value: int) -> int:
        return value

    response = TestClient(app).get("/n/SECRET-VALUE")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["field"] == "path.value"
    assert "SECRET-VALUE" not in json.dumps(body)


def test_unknown_route_and_method_use_the_same_envelope():
    client = make_client(RuntimeError())
    assert client.get("/nope").json()["error"]["code"] == "route_not_found"
    assert client.post("/boom").json()["error"]["code"] == "method_not_allowed"
