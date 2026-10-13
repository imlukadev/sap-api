from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sap_gateway.errors import ContractMappingError
from sap_gateway.services.maintenance_notification import contracts as soap
from sap_gateway.services.maintenance_notification import mappers
from sap_gateway.services.maintenance_notification import schemas as rest


def test_code_tables_are_exhaustive_and_bijective():
    assert set(mappers.NOTIFICATION_TYPE_TO_SOAP) == set(rest.NotificationType)
    assert set(mappers.PRIORITY_TO_SOAP) == set(rest.Priority)
    assert set(mappers.STATUS_TO_SOAP) == set(rest.NotificationStatus)
    assert set(mappers.NOTIFICATION_TYPE_FROM_SOAP) == set(soap.NotificationTypeCode)
    assert set(mappers.PRIORITY_FROM_SOAP) == set(soap.PriorityCode)
    assert set(mappers.STATUS_FROM_SOAP) == set(soap.StatusCode)
    assert set(mappers.SEVERITY_FROM_SOAP) == set(soap.MessageSeverity)
    assert len(set(mappers.PRIORITY_TO_SOAP.values())) == len(rest.Priority)


def test_create_request_maps_nested_structure_and_enums():
    body = rest.NotificationCreate.model_validate(
        {
            "notification_type": "malfunction_report",
            "description": "Vazamento",
            "priority": "very_high",
            "plant": "1000",
            "required_start": "2026-05-01",
            "planned_cost": "99.90",
            "items": [
                {"item_number": 1, "description": "a", "quantity": "2"},
                {"item_number": 2},
            ],
            "long_text": ["l1", "l2"],
            "attributes": [{"name": "k", "value": None}],
        }
    )
    request = mappers.create_request_to_soap(body)
    assert request.header.notification_type is soap.NotificationTypeCode.M2
    assert request.header.priority is soap.PriorityCode.P1
    assert request.header.planned_cost == Decimal("99.90")
    assert request.header.required_start == date(2026, 5, 1)
    assert [i.item_number for i in request.items.item] == [1, 2]
    assert request.items.item[1].model_fields_set == {"item_number"}
    assert request.long_text.text_line == ["l1", "l2"]
    assert request.attributes.attribute[0].value is None


def test_create_request_preserves_absent_null_and_empty():
    body = rest.NotificationCreate.model_validate(
        {
            "notification_type": "activity_report",
            "description": "x",
            "plant": "1000",
            "reported_by": None,
            "functional_location": "",
            "items": [],
        }
    )
    request = mappers.create_request_to_soap(body)
    fields = request.header.model_fields_set
    assert "reported_by" in fields and request.header.reported_by is None
    assert request.header.functional_location == ""
    assert "priority" not in fields and "planned_cost" not in fields
    assert request.items is not None and request.items.item == []
    assert request.long_text is None and request.attributes is None


def test_search_query_maps_only_given_filters_and_always_sends_max_results():
    query = rest.NotificationSearchQuery(status="in_process", max_results=10)
    request = mappers.search_query_to_soap(query)
    assert request.status is soap.StatusCode.NOPR
    assert request.max_results == 10
    assert (
        "plant" not in request.model_fields_set and "created_from" not in request.model_fields_set
    )


def _soap_notification(**header_overrides) -> soap.NotificationData:
    return soap.NotificationData.model_validate(
        {
            "NotificationId": "000000000007",
            "Header": {
                "NotificationType": "M3",
                "Description": "desc",
                "Plant": "1000",
                **header_overrides,
            },
            "Status": "NOCO",
            "CreatedOn": date(2026, 1, 2),
            "CreatedAt": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            "Items": {"Item": [{"ItemNumber": 1, "DamageCode": None, "Description": ""}]},
            "LongText": {"TextLine": ["x"]},
            "Attributes": {"Attribute": [{"Name": "n", "Value": None}]},
        }
    )


def test_notification_from_soap_flattens_and_translates_codes():
    out = mappers.notification_from_soap(_soap_notification(Priority="4"))
    assert out.notification_type is rest.NotificationType.ACTIVITY_REPORT
    assert out.status is rest.NotificationStatus.COMPLETED
    assert out.priority is rest.Priority.LOW
    assert out.items[0].model_fields_set == {"item_number", "damage_code", "description"}
    assert out.items[0].damage_code is None and out.items[0].description == ""
    assert out.long_text == ["x"]
    assert out.attributes[0].value is None


def test_notification_from_soap_preserves_tristate_in_json():
    out = mappers.notification_from_soap(_soap_notification(ReportedBy=None, FunctionalLocation=""))
    dumped = out.model_dump(mode="json", exclude_unset=True)
    assert dumped["reported_by"] is None
    assert dumped["functional_location"] == ""
    assert "planned_cost" not in dumped and "priority" not in dumped


def test_absent_collections_become_empty_arrays():
    data = soap.NotificationData.model_validate(
        {
            "NotificationId": "000000000007",
            "Header": {"NotificationType": "M1", "Description": "d", "Plant": "1000"},
            "Status": "OSNO",
            "CreatedOn": date(2026, 1, 2),
            "CreatedAt": datetime(2026, 1, 2, tzinfo=UTC),
        }
    )
    out = mappers.notification_from_soap(data)
    assert out.items == [] and out.long_text == [] and out.attributes == []


def test_created_and_search_results_from_soap():
    created = mappers.created_from_soap(
        soap.NotificationCreateResponse.model_validate(
            {
                "NotificationId": "000000000009",
                "Messages": {"Message": [{"Severity": "W", "Text": "aviso"}]},
            }
        )
    )
    assert created.messages[0].severity is rest.MessageSeverity.WARNING
    empty = mappers.search_result_from_soap(
        soap.NotificationSearchResponse.model_validate({"Notifications": {}, "Truncated": False})
    )
    assert empty.notifications == [] and empty.truncated is False
    result = mappers.search_result_from_soap(
        soap.NotificationSearchResponse.model_validate(
            {
                "Notifications": {
                    "Notification": [
                        {
                            "NotificationId": "000000000001",
                            "Description": "a",
                            "Status": "OSNO",
                            "CreatedOn": date(2026, 1, 1),
                        },
                        {
                            "NotificationId": "000000000002",
                            "Description": "b",
                            "Status": "NOPR",
                            "Priority": "1",
                            "CreatedOn": date(2026, 1, 1),
                        },
                    ]
                },
                "Truncated": True,
            }
        )
    )
    assert [n.status for n in result.notifications] == [
        rest.NotificationStatus.OUTSTANDING,
        rest.NotificationStatus.IN_PROCESS,
    ]
    assert "priority" not in result.notifications[0].model_fields_set
    assert result.notifications[1].priority is rest.Priority.VERY_HIGH


def test_unknown_code_in_lookup_fails_explicitly():
    with pytest.raises(ContractMappingError):
        mappers._lookup(mappers.STATUS_FROM_SOAP, "BOGUS")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        {"notification_type": "x", "description": "d", "plant": "1000"},
        {"notification_type": "activity_report", "description": "", "plant": "1000"},
        {"notification_type": "activity_report", "description": "d" * 41, "plant": "1000"},
        {"notification_type": "activity_report", "description": "d", "plant": "10"},
        {"notification_type": "activity_report", "description": "d", "plant": "1000", "bogus": 1},
        {
            "notification_type": "activity_report",
            "description": "d",
            "plant": "1000",
            "items": [{"item_number": 0}],
        },
        {
            "notification_type": "activity_report",
            "description": "d",
            "plant": "1000",
            "planned_cost": "-1",
        },
    ],
)
def test_rest_validation_rejects_invalid_payloads(payload):
    with pytest.raises(ValidationError):
        rest.NotificationCreate.model_validate(payload)


def test_search_query_period_validation():
    with pytest.raises(ValueError, match="created_from"):
        rest.NotificationSearchQuery(created_from=date(2026, 2, 1), created_to=date(2026, 1, 1))
