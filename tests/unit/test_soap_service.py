"""``SapModel`` e ``SoapService`` em nível unitário (sem rede)."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

import pytest
from pydantic import Field
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, ReadTimeout, SSLError
from urllib3.exceptions import MaxRetryError, NewConnectionError
from zeep.exceptions import Fault, TransportError, XMLParseError

from helpers import build_settings
from sap_gateway.config import PACKAGED_WSDL, SoapServiceSettings
from sap_gateway.errors import AuthenticationFailedError, AuthorizationError
from sap_gateway.services.maintenance_notification import contracts as c
from sap_gateway.soap.errors import (
    SoapConnectionError,
    SoapFaultError,
    SoapHttpError,
    SoapRequestError,
    SoapResponseError,
    SoapTimeoutError,
    SoapUnavailableError,
)
from sap_gateway.soap.models import SapModel, SapStr
from sap_gateway.soap.service import SoapService, _fault_error, _translate, _xml_to_dict
from sap_gateway.soap.transport import RequestTransport, UpstreamHttpError

NS = c.NAMESPACE


def make_service(**cfg) -> SoapService:
    settings = build_settings("http://127.0.0.1:1/x", **cfg)
    return SoapService(
        c.SERVICE_KEY,
        c.BINDING,
        settings.soap_service(c.SERVICE_KEY),
        default_wsdl=PACKAGED_WSDL / "maintenance_notification.wsdl",
        require_https=False,
    )


def test_aliases_are_generated_in_pascal_case_and_accept_python_names():
    request = c.ReadRequest.model_validate({"NotificationId": "1"})
    assert request.notification_id == "1"
    assert c.ReadRequest(notification_id="1").to_soap() == {"NotificationId": "1"}


def test_explicit_alias_overrides_the_generator_for_acronyms():
    class Selection(SapModel):
        id: str = Field(alias="ID")
        language_code: str

    assert Selection(id="X", language_code="PT").to_soap() == {"ID": "X", "LanguageCode": "PT"}
    assert Selection.model_validate({"ID": "X", "LanguageCode": "PT"}).id == "X"


def test_to_soap_is_recursive_omits_none_and_keeps_native_types():
    request = c.CreateRequest(
        header=c.Header(
            notification_type="M1",
            description="d",
            plant="1000",
            required_start=date(2026, 1, 2),
            planned_cost=Decimal("10.50"),
        ),
        items=c.ItemList(
            item=[c.Item(item_number=1), c.Item(item_number=2, quantity=Decimal("1.5"))]
        ),
    )
    args = request.to_soap()
    assert args["Header"] == {
        "NotificationType": "M1",
        "Description": "d",
        "Plant": "1000",
        "RequiredStart": date(2026, 1, 2),
        "PlannedCost": Decimal("10.50"),
    }
    assert args["Items"]["Item"][1] == {"ItemNumber": 2, "Quantity": Decimal("1.5")}
    assert "LongText" not in args


def test_enums_are_serialised_as_values():
    class Color(StrEnum):
        RED = "R"

    class Model(SapModel):
        color: Color

    assert Model(color=Color.RED).to_soap() == {"Color": Color.RED}  # StrEnum já é str


def test_sapstr_turns_the_none_that_zeep_returns_for_empty_elements_into_empty_string():
    class Model(SapModel):
        name: SapStr

    assert Model.model_validate({"Name": None}).name == ""
    assert Model.model_validate({"Name": "x"}).name == "x"


def test_responses_ignore_extra_fields_returned_by_sap():
    assert c.ReadRequest.model_validate({"NotificationId": "1", "Extra": 1}).notification_id == "1"


def test_xml_request_shows_the_generated_envelope_without_network():
    service = make_service()
    xml = service.xml_request("NotificationRead", c.ReadRequest(notification_id="000000000001"))
    assert "<ns0:NotificationId>000000000001</ns0:NotificationId>" in xml
    xml = service.xml_request("NotificationRead", NotificationId="000000000002")
    assert "000000000002" in xml
    assert service.contract_loaded


def test_wsdl_failure_is_503_and_not_cached(tmp_path):
    service = make_service(wsdl=str(tmp_path / "missing.wsdl"))
    with pytest.raises(SoapUnavailableError):
        service.xml_request("NotificationRead", NotificationId="1")
    assert not service.contract_loaded


def test_wsdl_without_endpoint_or_https_is_refused_when_required():
    cfg = SoapServiceSettings(endpoint_url="http://sap.example/svc")
    service = SoapService(
        "s",
        c.BINDING,
        cfg,
        default_wsdl=PACKAGED_WSDL / "maintenance_notification.wsdl",
        require_https=True,
    )
    doc = service._load_document(None)
    with pytest.raises(SoapUnavailableError):
        service._address(doc)
    relaxed = SoapService(
        "s",
        c.BINDING,
        cfg,
        default_wsdl=PACKAGED_WSDL / "maintenance_notification.wsdl",
        require_https=False,
    )
    assert relaxed._address(doc) == "http://sap.example/svc"


def test_wsdl_address_is_used_when_no_endpoint_is_configured():
    service = make_service(endpoint_url=None)
    assert service._address(service._load_document(None)).endswith("/soap/maintenance-notification")


def transport() -> RequestTransport:
    import requests

    return RequestTransport(requests.Session(), timeouts=(1, 1))


@pytest.mark.parametrize(
    ("exc", "expected_type", "attrs"),
    [
        (UpstreamHttpError(401), AuthenticationFailedError, {}),
        (UpstreamHttpError(403), AuthorizationError, {}),
        (UpstreamHttpError(404), SoapHttpError, {"upstream_status": 404}),
        (UpstreamHttpError(302), SoapHttpError, {"upstream_status": 302}),
        (ConnectTimeout(), SoapTimeoutError, {"request_sent": False}),
        (ReadTimeout(), SoapTimeoutError, {"request_sent": True}),
        (SSLError(), SoapConnectionError, {"request_sent": False, "transient": False}),
        (
            RequestsConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused"))),
            SoapConnectionError,
            {"request_sent": False},
        ),
        (RequestsConnectionError("reset by peer"), SoapConnectionError, {"request_sent": True}),
        (TransportError("x", status_code=200), SoapResponseError, {}),
        (TransportError("x", status_code=500), SoapHttpError, {"upstream_status": 500}),
    ],
)
def test_transport_exceptions_are_translated_without_keeping_content(exc, expected_type, attrs):
    error = _translate(exc, transport())
    assert type(error) is expected_type
    for name, value in attrs.items():
        assert getattr(error, name) == value


def test_zeep_errors_depend_on_whether_a_response_was_received():
    t = transport()
    assert isinstance(_translate(XMLParseError("x"), t), SoapRequestError)
    t.response_received = True
    assert isinstance(_translate(XMLParseError("x"), t), SoapResponseError)
    with pytest.raises(RuntimeError, match="boom"):
        _translate(RuntimeError("boom"), t)


def test_fault_with_detail_becomes_soap_fault_with_searchable_detail():
    from lxml import etree

    detail = etree.fromstring(
        f'<detail><n:NotificationFault xmlns:n="{NS}"><n:Code>X1</n:Code><n:Message>m</n:Message></n:NotificationFault></detail>'
    )
    error = _fault_error(Fault("secret faultstring", code="soap:Server", detail=detail))
    assert isinstance(error, SoapFaultError)
    assert error.find("Code") == "X1" and error.find("Missing") is None
    assert error.fault_code == "soap:Server"
    assert "secret" not in error.public_message


def test_fault_without_detail_is_still_a_soap_fault_but_zeeps_unknown_fault_is_http():
    assert isinstance(_fault_error(Fault("falha", code="soap:Client", detail=None)), SoapFaultError)
    assert isinstance(
        _fault_error(Fault("Unknown fault occured", detail=b"<html/>")), SoapHttpError
    )
    assert isinstance(_fault_error(Fault("Unknown fault occured", detail="<html/>")), SoapHttpError)


def test_xml_to_dict_is_namespace_agnostic_and_nested():
    from lxml import etree

    node = etree.fromstring('<d xmlns:a="urn:a"><a:X><a:Y> v </a:Y></a:X><Z/></d>')
    assert _xml_to_dict(node) == {"X": {"Y": "v"}, "Z": ""}
