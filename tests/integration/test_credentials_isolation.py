"""Credenciais, cookies e sessão por usuário: encaminhamento correto e zero vazamento."""

import asyncio
import base64

import httpx2 as httpx
import pytest

from helpers import USERS
from sap_gateway.devtools import mock_sap as m
from sap_gateway.soap import gateway as gateway_module

BASE = "/api/v1/notifications"


def basic_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_basic_credentials_are_forwarded_as_basic_to_soap(client, mock_sap, basic_auth):
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    sent = mock_sap.mock.recorded()[-1]
    assert sent.auth_mechanism == "basic" and sent.username == "alice"
    assert sent.authorization == basic_header("alice", USERS["alice"])
    assert b"Password" not in sent.body and USERS["alice"].encode() not in sent.body


def test_ws_security_mode_sends_username_token_instead_of_basic(make_app, mock_sap, basic_auth):
    from fastapi.testclient import TestClient

    bundle = make_app(auth_mode="ws_security_username_token")
    with TestClient(bundle.app) as client:
        response = client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("bob"))
    assert response.status_code == 200
    sent = mock_sap.mock.recorded()[-1]
    assert sent.auth_mechanism == "wsse" and sent.username == "bob"
    assert sent.authorization is None
    assert b"UsernameToken" in sent.body


def test_no_session_state_is_carried_between_requests(client, mock_sap, basic_auth):
    for user in ("alice", "bob", "alice"):
        assert client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth(user)).status_code == 200
    recorded = mock_sap.mock.recorded()
    assert [r.username for r in recorded] == ["alice", "bob", "alice"]
    assert all(r.cookie is None for r in recorded), "SAP_SESSIONID de uma chamada vazou para outra"


def test_every_attempt_uses_a_fresh_closed_session(client, mock_sap, basic_auth, monkeypatch):
    seen: list[tuple[object, str, list[bool]]] = []
    original = gateway_module.build_request_session

    def spy(cfg, credentials):
        session = original(cfg, credentials)
        closed = [False]
        real_close = session.close

        def close() -> None:
            closed[0] = True
            real_close()

        session.close = close  # type: ignore[method-assign]
        seen.append((session, session.auth.username, closed))
        return session

    monkeypatch.setattr(gateway_module, "build_request_session", spy)
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("alice"))
    client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth("bob"))
    client.get(f"{BASE}/{m.ID_HTTP_503}", auth=basic_auth("alice"))
    assert len(seen) == 5
    assert len({id(session) for session, _, _ in seen}) == 5  # referências mantidas: ids únicos
    assert [u for _, u, _ in seen] == ["alice", "bob", "alice", "alice", "alice"]
    assert all(closed[0] for _, _, closed in seen)


async def _run_concurrently(app, requests):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.gather(
            *(
                client.get(path, headers={"Authorization": basic_header(*creds)})
                for path, creds in requests
            )
        )


def test_concurrent_requests_from_different_users_do_not_share_credentials(bundle, mock_sap):
    in_flight = 0
    max_in_flight = 0
    barrier: asyncio.Barrier | None = None

    async def hook(recorded):
        nonlocal in_flight, max_in_flight, barrier
        barrier = barrier or asyncio.Barrier(2)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        finally:
            in_flight -= 1

    mock_sap.mock.hook = hook
    ids = {"alice": m.ID_SAMPLE_FULL, "bob": m.ID_SAMPLE_TRISTATE}
    requests = []
    for _ in range(6):
        for user in ("alice", "bob"):
            requests.append((f"{BASE}/{ids[user]}", (user, USERS[user])))

    responses = asyncio.run(_run_concurrently(bundle.app, requests))

    assert max_in_flight >= 2, (
        "as requisições não se sobrepuseram — o teste não provaria isolamento"
    )
    assert all(r.status_code == 200 for r in responses)
    for (path, (user, _)), response in zip(requests, responses, strict=True):
        assert response.json()["notification_id"] == ids[user]
        assert path.endswith(ids[user])
    recorded = mock_sap.mock.recorded()
    assert len(recorded) == 12
    for request in recorded:
        expected_id = ids[request.username].encode()
        assert expected_id in request.body, "credencial de um usuário usada na operação de outro"
        assert request.authorization == basic_header(request.username, USERS[request.username])
        assert request.cookie is None


def test_failure_of_one_request_does_not_affect_another(bundle, mock_sap):
    requests = [
        (f"{BASE}/{m.ID_NOT_FOUND}", ("alice", USERS["alice"])),
        (f"{BASE}/{m.ID_SAMPLE_FULL}", ("bob", USERS["bob"])),
        (f"{BASE}/{m.ID_SAMPLE_FULL}", ("alice", "wrong-password")),
        (f"{BASE}/{m.ID_HTTP_500}", ("bob", USERS["bob"])),
        (f"{BASE}/{m.ID_SAMPLE_TRISTATE}", ("alice", USERS["alice"])),
        (f"{BASE}/{m.ID_SAMPLE_FULL}", ("bob", USERS["bob"])),
    ]
    responses = asyncio.run(_run_concurrently(bundle.app, requests))
    assert [r.status_code for r in responses] == [404, 200, 401, 502, 200, 200]
    followup = asyncio.run(
        _run_concurrently(bundle.app, [(f"{BASE}/{m.ID_SAMPLE_FULL}", ("alice", USERS["alice"]))])
    )
    assert followup[0].status_code == 200
    assert mock_sap.mock.recorded()[-1].username == "alice"


def test_request_context_does_not_leak_across_requests(bundle, mock_sap):
    from sap_gateway import context

    asyncio.run(
        _run_concurrently(bundle.app, [(f"{BASE}/{m.ID_SAMPLE_FULL}", ("alice", USERS["alice"]))])
    )
    assert context.current_request() is None and context.current_soap() is None


@pytest.mark.parametrize("user", ["alice", "bob"])
def test_password_never_appears_in_any_telemetry_or_response(client, bundle, basic_auth, user):
    password = USERS[user]
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    responses = [
        client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=basic_auth(user)),
        client.get(f"{BASE}/{m.ID_NOT_FOUND}", auth=basic_auth(user)),
        client.get(f"{BASE}/{m.ID_HTTP_500}", auth=basic_auth(user)),
        client.get(f"{BASE}/{m.ID_SAMPLE_FULL}", auth=(user, "wrong")),
    ]
    collected = bundle.collected
    haystacks = [r.text + str(r.headers) for r in responses]
    haystacks.append(collected.stream.getvalue())
    haystacks += [
        str(span.attributes) + span.name + str(span.events) + str(span.status)
        for span in collected.spans.get_finished_spans()
    ]
    haystacks += [
        str(rec.log_record.attributes) + str(rec.log_record.body)
        for rec in collected.logs.get_finished_logs()
    ]
    haystacks += [str(points) for points in collected.metric_points().values()]
    for haystack in haystacks:
        assert password not in haystack
        assert token not in haystack
        assert "Authorization" not in haystack or "Basic" not in haystack


def test_high_concurrency_with_mixed_operations_is_stable_and_isolated(bundle, mock_sap):
    """Muitas threads usando o mesmo ``Document`` WSDL compartilhado, com usuários e operações misturados."""
    import json

    def creation(user: str, n: int):
        body = {
            "notification_type": "activity_report",
            "description": f"{user}-{n}",
            "plant": "1000",
        }
        return ("POST", BASE, user, body)

    plan = []
    for n in range(20):
        for user in ("alice", "bob"):
            plan.append(("GET", f"{BASE}/{m.ID_SAMPLE_FULL}", user, None))
            plan.append(("GET", f"{BASE}?plant=1000&max_results=5", user, None))
            plan.append(creation(user, n))

    async def run():
        transport = httpx.ASGITransport(app=bundle.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(
                *(
                    client.request(
                        method,
                        path,
                        headers={"Authorization": basic_header(user, USERS[user])},
                        json=body,
                    )
                    for method, path, user, body in plan
                )
            )

    responses = asyncio.run(run())
    expected = {"GET": 200, "POST": 201}
    assert [r.status_code for r in responses] == [expected[method] for method, *_ in plan]

    created = [r for r in mock_sap.mock.recorded() if r.operation == "NotificationCreate"]
    assert len(created) == 40
    for request in created:
        description = (
            request.body.split(b"<ns0:Description>")[1].split(b"</ns0:Description>")[0].decode()
        )
        assert description.startswith(request.username + "-"), "criação atribuída ao usuário errado"
        assert request.authorization == basic_header(request.username, USERS[request.username])
        assert request.cookie is None
    assert len(mock_sap.mock.recorded()) == len(plan)
    assert json.loads(responses[0].text)["notification_id"] == m.ID_SAMPLE_FULL
