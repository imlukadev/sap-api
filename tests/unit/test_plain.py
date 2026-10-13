"""Conversão Zeep → estrutura simples preservando ausente / nil / vazio, com namespaces variados."""

import pytest
from lxml import etree
from zeep import Client

from sap_gateway.errors import ContractMappingError
from sap_gateway.services.maintenance_notification import contracts as c
from sap_gateway.soap.plain import to_plain

NS = c.NAMESPACE
XSI = "http://www.w3.org/2001/XMLSchema-instance"


@pytest.fixture(scope="module")
def client() -> Client:
    return Client(str(c.SERVICE.packaged_wsdl_path()))


def parse(client: Client, xml: str, element: str = "NotificationReadResponse"):
    node = etree.fromstring(xml)
    value = client.get_element(f"{{{NS}}}{element}").parse(node, client.wsdl.types)
    return to_plain(value, node)


def read_response(inner: str, prefix: str = "n") -> str:
    decl = f'xmlns:{prefix}="{NS}"' if prefix else f'xmlns="{NS}"'
    p = f"{prefix}:" if prefix else ""
    return (
        f'<{p}NotificationReadResponse {decl} xmlns:xsi="{XSI}"><{p}Notification>'
        f"{inner}</{p}Notification></{p}NotificationReadResponse>"
    )


def base_fields(p: str = "n:") -> str:
    return (
        f"<{p}NotificationId>000000000001</{p}NotificationId>"
        f"<{p}Header><{p}NotificationType>M1</{p}NotificationType><{p}Description>d</{p}Description>"
        f"<{p}Plant>1000</{p}Plant></{p}Header>"
        f"<{p}Status>OSNO</{p}Status><{p}CreatedOn>2026-01-02</{p}CreatedOn>"
        f"<{p}CreatedAt>2026-01-02T10:20:30Z</{p}CreatedAt>"
    )


def test_absent_elements_do_not_appear_in_the_result(client):
    plain = parse(client, read_response(base_fields()))
    notification = plain["Notification"]
    assert "Items" not in notification and "LongText" not in notification
    assert "ReportedBy" not in notification["Header"]


def test_nil_empty_and_absent_are_three_different_results(client):
    header = (
        "<n:Header><n:NotificationType>M1</n:NotificationType><n:Description></n:Description>"
        '<n:Plant>1000</n:Plant><n:FunctionalLocation/><n:ReportedBy xsi:nil="true"/></n:Header>'
    )
    inner = (
        "<n:NotificationId>000000000001</n:NotificationId>"
        + header
        + "<n:Status>OSNO</n:Status><n:CreatedOn>2026-01-02</n:CreatedOn>"
        "<n:CreatedAt>2026-01-02T10:20:30Z</n:CreatedAt>"
    )
    plain = parse(client, read_response(inner))["Notification"]["Header"]
    assert plain["Description"] == ""
    assert plain["FunctionalLocation"] == ""
    assert plain["ReportedBy"] is None
    assert "PlannedCost" not in plain


def test_empty_complex_element_differs_from_absent(client):
    plain = parse(client, read_response(base_fields() + "<n:Items/><n:LongText/>"))["Notification"]
    assert plain["Items"] == {}
    assert plain["LongText"] == {}
    model = c.NotificationData.from_plain(plain)
    assert model.items is not None and model.items.item == []
    assert "items" in model.model_fields_set
    assert (
        c.NotificationData.from_plain(
            parse(client, read_response(base_fields()))["Notification"]
        ).items
        is None
    )


def test_repeated_elements_and_nested_nil_items(client):
    items = (
        "<n:Items>"
        "<n:Item><n:ItemNumber>1</n:ItemNumber><n:Description/><n:DamageCode xsi:nil='true'/></n:Item>"
        "<n:Item><n:ItemNumber>2</n:ItemNumber><n:Description>x</n:Description><n:Quantity>2.50</n:Quantity></n:Item>"
        "</n:Items><n:LongText><n:TextLine>a</n:TextLine><n:TextLine></n:TextLine><n:TextLine>c</n:TextLine></n:LongText>"
    )
    plain = parse(client, read_response(base_fields() + items))["Notification"]
    first, second = plain["Items"]["Item"]
    assert first == {"ItemNumber": 1, "Description": "", "DamageCode": None}
    assert second["Quantity"] is not None and "DamageCode" not in second
    assert plain["LongText"]["TextLine"] == ["a", "", "c"]


@pytest.mark.parametrize("prefix", ["n", "ns7", "", "SAP"])
def test_result_is_independent_of_namespace_prefix(client, prefix):
    p = f"{prefix}:" if prefix else ""
    plain = parse(client, read_response(base_fields(p), prefix))
    assert plain["Notification"]["NotificationId"] == "000000000001"
    assert plain["Notification"]["Header"]["Plant"] == "1000"


def test_scalars_keep_their_python_types(client):
    from datetime import date, datetime
    from decimal import Decimal

    items = "<n:Items><n:Item><n:ItemNumber>7</n:ItemNumber><n:Quantity>1.250</n:Quantity></n:Item></n:Items>"
    plain = parse(client, read_response(base_fields() + items))["Notification"]
    assert plain["CreatedOn"] == date(2026, 1, 2)
    assert isinstance(plain["CreatedAt"], datetime) and plain["CreatedAt"].tzinfo is not None
    assert plain["Items"]["Item"][0]["ItemNumber"] == 7
    assert plain["Items"]["Item"][0]["Quantity"] == Decimal("1.250")


def test_collection_wrappers_in_other_responses(client):
    empty = parse(
        client,
        f'<n:NotificationSearchResponse xmlns:n="{NS}"><n:Notifications/><n:Truncated>false</n:Truncated></n:NotificationSearchResponse>',
        "NotificationSearchResponse",
    )
    assert empty == {"Notifications": {}, "Truncated": False}
    none = parse(
        client,
        f'<n:NotificationSearchResponse xmlns:n="{NS}"><n:Truncated>true</n:Truncated></n:NotificationSearchResponse>',
        "NotificationSearchResponse",
    )
    assert none == {"Truncated": True}
    assert c.NotificationSearchResponse.from_plain(empty).notifications is not None
    assert c.NotificationSearchResponse.from_plain(none).notifications is None


def test_structure_outside_contract_fails_explicitly(client):
    plain = parse(client, read_response(base_fields().replace("OSNO", "ZZZZ")))["Notification"]
    with pytest.raises(ContractMappingError) as excinfo:
        c.NotificationData.from_plain(plain)
    assert excinfo.value.details[0].field == "Status"


def test_empty_non_string_scalar_is_reported_not_coerced(client):
    items = "<n:Items><n:Item><n:ItemNumber/></n:Item></n:Items>"
    plain = parse(client, read_response(base_fields() + items))["Notification"]
    with pytest.raises(ContractMappingError):
        c.NotificationData.from_plain(plain)
