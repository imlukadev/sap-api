"""Aliases, validação e serialização dos contratos SOAP — incluindo ausente × nil × vazio."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from lxml import etree
from zeep import Client

from sap_gateway.errors import ContractMappingError
from sap_gateway.services.maintenance_notification import contracts as c
from sap_gateway.soap.contracts import SoapModel

NS = {"n": c.NAMESPACE, "xsi": "http://www.w3.org/2001/XMLSchema-instance"}


@pytest.fixture(scope="module")
def zeep_client() -> Client:
    return Client(str(c.SERVICE.packaged_wsdl_path()))


def render(zeep_client: Client, request: c.NotificationCreateRequest) -> etree._Element:
    binding = zeep_client.wsdl.bindings[c.SERVICE.binding_qname]
    service = zeep_client.create_service(c.SERVICE.binding_qname, "http://unused/")
    del binding
    return zeep_client.create_message(service, "NotificationCreate", **request.to_soap_args())


def test_aliases_follow_xsd_names_and_python_names_are_accepted():
    model = c.NotificationReadRequest.model_validate({"NotificationId": "000000000001"})
    assert model.notification_id == "000000000001"
    assert c.NotificationReadRequest(notification_id="000000000001").to_soap_args() == {
        "NotificationId": "000000000001"
    }
    assert c.NotificationHeader.model_fields["planned_cost"].alias == "PlannedCost"
    assert c.AttributeList.model_fields["attribute"].alias == "Attribute"


def test_unknown_fields_are_rejected_instead_of_dropped():
    with pytest.raises(ValueError, match="Extra inputs"):
        c.NotificationReadRequest.model_validate({"NotificationId": "000000000001", "Bogus": 1})


@pytest.mark.parametrize("value", ["1", "00000000000A", "0000000000001", ""])
def test_notification_id_pattern_is_enforced(value):
    with pytest.raises(ValueError, match="pattern"):
        c.NotificationReadRequest(notification_id=value)


def test_header_constraints():
    with pytest.raises(ValueError, match="at most 40"):
        c.NotificationHeader(notification_type="M1", description="x" * 41, plant="1000")
    with pytest.raises(ValueError, match="plant|Plant|at least 4"):
        c.NotificationHeader(notification_type="M1", description="ok", plant="10")
    with pytest.raises(ValueError, match="Input should be"):
        c.NotificationHeader(notification_type="M9", description="ok", plant="1000")


def test_models_are_immutable():
    request = c.NotificationReadRequest(notification_id="000000000001")
    with pytest.raises(ValueError, match="frozen"):
        request.notification_id = "000000000002"  # type: ignore[misc]


def test_to_soap_args_distinguishes_unset_none_and_empty():
    from zeep.xsd import Nil

    header = c.NotificationHeader(
        notification_type=c.NotificationTypeCode.M1,
        description="",
        plant="1000",
        reported_by=None,
        functional_location="",
    )
    args = header.to_soap_args()
    assert args["ReportedBy"] is Nil
    assert args["FunctionalLocation"] == ""
    assert "PlannedCost" not in args
    assert "Priority" not in args
    assert args["NotificationType"] == "M1"


def test_to_soap_args_is_recursive_over_objects_lists_and_enums():
    request = c.NotificationCreateRequest(
        header=c.NotificationHeader(
            notification_type="M2",
            description="d",
            plant="1000",
            priority="2",
            required_start=date(2026, 1, 2),
            planned_cost=Decimal("10.50"),
        ),
        items=c.NotificationItemList(
            item=[
                c.NotificationItem(item_number=1),
                c.NotificationItem(item_number=2, quantity=Decimal("1.5")),
            ]
        ),
        long_text=c.LongText(text_line=["a", "b"]),
    )
    args = request.to_soap_args()
    assert args["Header"]["Priority"] == "2"
    assert args["Header"]["RequiredStart"] == date(2026, 1, 2)
    assert args["Items"]["Item"][1] == {"ItemNumber": 2, "Quantity": Decimal("1.5")}
    assert args["LongText"] == {"TextLine": ["a", "b"]}
    assert "Attributes" not in args


def test_rendered_xml_reflects_absent_nil_empty_and_empty_collection(zeep_client):
    request = c.NotificationCreateRequest(
        header=c.NotificationHeader(
            notification_type="M1",
            description="",
            plant="1000",
            reported_by=None,
            planned_cost=None,
        ),
        items=c.NotificationItemList(
            item=[c.NotificationItem(item_number=1, description="", damage_code=None)]
        ),
        long_text=c.LongText(text_line=[]),
    )
    root = render(zeep_client, request).find(".//n:NotificationCreateRequest", NS)
    header = root.find("n:Header", NS)
    assert not header.find("n:Description", NS).text
    assert header.find("n:Description", NS).get(f"{{{NS['xsi']}}}nil") is None  # vazio, não nil
    assert header.find("n:ReportedBy", NS).get(f"{{{NS['xsi']}}}nil") == "true"
    assert header.find("n:PlannedCost", NS).get(f"{{{NS['xsi']}}}nil") == "true"
    assert header.find("n:FunctionalLocation", NS) is None  # ausente
    item = root.find("n:Items/n:Item", NS)
    assert item.find("n:DamageCode", NS).get(f"{{{NS['xsi']}}}nil") == "true"
    assert item.find("n:Quantity", NS) is None
    assert root.find("n:LongText", NS) is not None and len(root.find("n:LongText", NS)) == 0
    assert root.find("n:Attributes", NS) is None


def test_decimal_date_and_datetime_roundtrip_through_zeep(zeep_client):
    request = c.NotificationCreateRequest(
        header=c.NotificationHeader(
            notification_type="M1",
            description="d",
            plant="1000",
            required_start=date(2026, 12, 31),
            planned_cost=Decimal("0.10"),
        )
    )
    root = render(zeep_client, request).find(".//n:Header", NS)
    assert root.findtext("n:RequiredStart", namespaces=NS) == "2026-12-31"
    assert root.findtext("n:PlannedCost", namespaces=NS) == "0.10"
    parsed = c.NotificationData.model_validate(
        {
            "NotificationId": "000000000001",
            "Header": {"NotificationType": "M1", "Description": "d", "Plant": "1000"},
            "Status": "OSNO",
            "CreatedOn": date(2026, 1, 1),
            "CreatedAt": datetime(2026, 1, 1, 10, 0, 0),
        }
    )
    assert parsed.items is None
    assert "items" not in parsed.model_fields_set


def test_from_plain_wraps_validation_errors_without_leaking_values():
    secret_like = "SENSITIVE-VALUE-123"
    with pytest.raises(ContractMappingError) as excinfo:
        c.NotificationReadRequest.from_plain({"NotificationId": secret_like})
    error = excinfo.value
    assert error.status_code == 502
    assert error.details and error.details[0].field == "NotificationId"
    assert secret_like not in repr(error.details)
    assert secret_like not in str(error)


def test_every_operation_declares_matching_element_names():
    for operation in (c.NOTIFICATION_READ, c.NOTIFICATION_CREATE, c.NOTIFICATION_SEARCH):
        assert issubclass(operation.request_model, SoapModel)
        assert operation.response_element_qname == f"{{{c.NAMESPACE}}}{operation.name}Response"
        assert operation.fault_element_qname == f"{{{c.NAMESPACE}}}NotificationFault"
    assert c.NOTIFICATION_CREATE.idempotent is False
    assert c.NOTIFICATION_READ.idempotent and c.NOTIFICATION_SEARCH.idempotent
