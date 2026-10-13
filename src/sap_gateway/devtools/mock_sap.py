"""Servidor SOAP simulado (estilo SAP) para desenvolvimento, demonstração e testes.

NÃO é parte da API de produção. Implementa o contrato ``maintenance_notification.wsdl`` e permite
provocar cenários de falha por *IDs mágicos* de nota. Registra cada requisição recebida
(cabeçalho ``Authorization``, cookies, SOAPAction) para que os testes comprovem o encaminhamento
das credenciais e a ausência de vazamento de sessão.

    MOCK_SAP_USERS="alice:s3cret,bob:other" uv run mock-sap
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from lxml import etree
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

NS = "urn:example:sap:pm:notification:v1"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SOAP_PATH = "/soap/maintenance-notification"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSDL_DIR = Path(__file__).resolve().parent.parent / "wsdl"

ID_NOT_FOUND = "000000000404"
ID_NOT_AUTHORIZED = "000000000403"
ID_HTTP_500 = "000000000500"
ID_HTTP_503 = "000000000503"
ID_SLOW = "000000000504"
ID_MALFORMED_XML = "000000000999"
ID_WRONG_ELEMENT = "000000000998"
ID_BAD_ENUM = "000000000997"
ID_SAMPLE_TRISTATE = "000000000001"
ID_SAMPLE_FULL = "000000000002"
PLANT_EMPTY_COLLECTION = "9999"
PLANT_NO_COLLECTION = "9998"

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    operation: str
    username: str | None
    auth_mechanism: str
    authorization: str | None
    cookie: str | None
    soap_action: str | None
    body: bytes
    headers: dict[str, str] = field(repr=False)


Hook = Callable[[RecordedRequest], Awaitable[None] | None]


class MockSapSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_SAP_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8081
    users: SecretStr = SecretStr("")
    slow_seconds: float = 5.0

    def parsed_users(self) -> dict[str, str]:
        pairs = (
            item.split(":", 1) for item in self.users.get_secret_value().split(",") if ":" in item
        )
        return {user.strip(): password for user, password in pairs}


def _q(local: str) -> str:
    return f"{{{NS}}}{local}"


def _el(parent: etree._Element, local: str, text: str | None = None) -> etree._Element:
    child = etree.SubElement(parent, _q(local))
    if text is not None:
        child.text = text
    return child


def _nil(parent: etree._Element, local: str) -> etree._Element:
    child = etree.SubElement(parent, _q(local))
    child.set(f"{{{XSI}}}nil", "true")
    return child


def _envelope(body_child: etree._Element) -> bytes:
    env = etree.Element(f"{{{SOAP_ENV}}}Envelope", nsmap={"soap": SOAP_ENV, "n": NS, "xsi": XSI})
    body = etree.SubElement(env, f"{{{SOAP_ENV}}}Body")
    body.append(body_child)
    return etree.tostring(env, xml_declaration=True, encoding="utf-8")


def _fault(code: str, message: str) -> Response:
    env = etree.Element(f"{{{SOAP_ENV}}}Envelope", nsmap={"soap": SOAP_ENV, "n": NS})
    body = etree.SubElement(env, f"{{{SOAP_ENV}}}Body")
    fault = etree.SubElement(body, f"{{{SOAP_ENV}}}Fault")
    etree.SubElement(fault, "faultcode").text = "soap:Server"
    etree.SubElement(fault, "faultstring").text = f"internal detail: {message}"
    detail = etree.SubElement(fault, "detail")
    nf = _el(detail, "NotificationFault")
    _el(nf, "Code", code)
    _el(nf, "Message", message)
    xml = etree.tostring(env, xml_declaration=True, encoding="utf-8")
    return Response(xml, status_code=500, media_type="text/xml; charset=utf-8")


def _text(node: etree._Element, local: str) -> str | None:
    child = node.find(_q(local))
    return None if child is None else (child.text or "")


class MockSapApp:
    def __init__(self, users: dict[str, str], *, slow_seconds: float = 5.0) -> None:
        self.users = dict(users)
        self.slow_seconds = slow_seconds
        self.requests: list[RecordedRequest] = []
        self.hook: Hook | None = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1000)
        self._created: dict[str, dict[str, Any]] = {}
        self._sessions = itertools.count(1)
        self.app = Starlette(
            routes=[
                Route(SOAP_PATH, self.handle, methods=["GET", "POST"]),
                Route("/soap/{name}.xsd", self.schema, methods=["GET"]),
            ],
        )

    def recorded(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self.requests)

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self._created.clear()
        self.hook = None

    def _check(self, user: str, password: str) -> str | None:
        expected = self.users.get(user)
        return user if expected is not None and expected == password else None

    def _authenticate_basic(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, _, password = base64.b64decode(token, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return self._check(user, password)

    def _authenticate_wsse(self, envelope: etree._Element) -> str | None:
        token = envelope.find(f".//{{{WSSE_NS}}}UsernameToken")
        if token is None:
            return None
        user = token.findtext(f"{{{WSSE_NS}}}Username") or ""
        password = token.findtext(f"{{{WSSE_NS}}}Password") or ""
        return self._check(user, password)

    @staticmethod
    def _unauthorized() -> Response:
        return Response(
            "<html><body>Logon failed</body></html>",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="SAP"'},
            media_type="text/html",
        )

    async def schema(self, request: Request) -> Response:
        if self._authenticate_basic(request) is None:
            return self._unauthorized()
        path = WSDL_DIR / f"{request.path_params['name']}.xsd"
        return Response(path.read_bytes(), media_type="text/xml")

    async def handle(self, request: Request) -> Response:
        if request.method == "GET":
            if self._authenticate_basic(request) is None:
                return self._unauthorized()
            return Response(
                (WSDL_DIR / "maintenance_notification.wsdl").read_bytes(), media_type="text/xml"
            )

        raw = await request.body()
        try:
            envelope = etree.fromstring(raw, parser=_PARSER)
            body = envelope.find(f"{{{SOAP_ENV}}}Body")
            assert body is not None and len(body)
            request_node = body[0]
            operation = etree.QName(request_node).localname.removesuffix("Request")
        except (etree.XMLSyntaxError, AssertionError):
            return PlainTextResponse("bad request", status_code=400)

        mechanism = "basic"
        username = self._authenticate_basic(request)
        if username is None and "authorization" not in request.headers:
            username = self._authenticate_wsse(envelope)
            mechanism = "wsse"
        if username is None:
            return self._unauthorized()

        recorded = RecordedRequest(
            operation=operation,
            username=username,
            auth_mechanism=mechanism,
            authorization=request.headers.get("authorization"),
            cookie=request.headers.get("cookie"),
            soap_action=request.headers.get("soapaction"),
            body=raw,
            headers=dict(request.headers),
        )
        with self._lock:
            self.requests.append(recorded)
        if self.hook is not None:
            outcome = self.hook(recorded)
            if asyncio.iscoroutine(outcome):
                await outcome

        handler = {
            "NotificationRead": self._read,
            "NotificationCreate": self._create,
            "NotificationSearch": self._search,
        }.get(operation)
        if handler is None:
            return _fault("UNKNOWN_OPERATION", operation)
        response = await handler(request_node)
        response.set_cookie("SAP_SESSIONID", f"{username}-{next(self._sessions)}", path="/")
        return response

    async def _read(self, node: etree._Element) -> Response:
        notification_id = _text(node, "NotificationId") or ""
        special = {
            ID_NOT_FOUND: lambda: _fault("NOTIFICATION_NOT_FOUND", "no such notification"),
            ID_NOT_AUTHORIZED: lambda: _fault("NOT_AUTHORIZED", "no authorization"),
            ID_HTTP_500: lambda: Response(
                "<html>boom</html>", status_code=500, media_type="text/html"
            ),
            ID_HTTP_503: lambda: Response("unavailable", status_code=503, media_type="text/plain"),
            ID_MALFORMED_XML: lambda: Response("<soap:Envelope><oops", media_type="text/xml"),
            ID_WRONG_ELEMENT: lambda: Response(
                _envelope(etree.Element(_q("Unexpected"))), media_type="text/xml"
            ),
        }
        if notification_id in special:
            return special[notification_id]()
        if notification_id == ID_SLOW:
            await asyncio.sleep(self.slow_seconds)
        response = etree.Element(_q("NotificationReadResponse"))
        data = _el(response, "Notification")
        if notification_id == ID_SAMPLE_TRISTATE:
            self._fill_tristate(data, notification_id)
        elif notification_id in (ID_SAMPLE_FULL, ID_SLOW):
            self._fill_full(data, notification_id)
        elif notification_id == ID_BAD_ENUM:
            self._fill_full(data, notification_id)
            data.find(_q("Status")).text = "XXXX"
        elif notification_id in self._created:
            self._fill_created(data, notification_id, self._created[notification_id])
        else:
            return _fault("NOTIFICATION_NOT_FOUND", "no such notification")
        return Response(_envelope(response), media_type="text/xml; charset=utf-8")

    @staticmethod
    def _header(parent: etree._Element, *, description: str | None) -> etree._Element:
        header = _el(parent, "Header")
        _el(header, "NotificationType", "M2")
        _el(header, "Description", description)
        return header

    def _fill_tristate(self, data: etree._Element, notification_id: str) -> None:
        """Mistura intencional: ausente, ``xsi:nil``, vazio e coleção vazia."""
        _el(data, "NotificationId", notification_id)
        header = self._header(data, description="")
        _el(header, "Plant", "1000")
        _nil(header, "ReportedBy")
        _nil(header, "PlannedCost")
        _el(data, "Status", "OSNO")
        _el(data, "CreatedOn", "2026-03-01")
        _el(data, "CreatedAt", "2026-03-01T10:20:30Z")
        items = _el(data, "Items")
        item = _el(items, "Item")
        _el(item, "ItemNumber", "1")
        _el(item, "Description", "")
        _nil(item, "DamageCode")
        _el(data, "LongText")

    def _fill_full(self, data: etree._Element, notification_id: str) -> None:
        _el(data, "NotificationId", notification_id)
        header = self._header(data, description="Bomba P-101 vazando")
        _el(header, "Priority", "2")
        _el(header, "Plant", "1000")
        _el(header, "FunctionalLocation", "1000-PUMP-101")
        _el(header, "ReportedBy", "OPERADOR1")
        _el(header, "RequiredStart", "2026-03-05")
        _el(header, "PlannedCost", "1234.50")
        _el(data, "Status", "NOPR")
        _el(data, "CreatedOn", "2026-03-01")
        _el(data, "CreatedAt", "2026-03-01T10:20:30Z")
        items = _el(data, "Items")
        for number, damage in ((1, "D01"), (2, "D02")):
            item = _el(items, "Item")
            _el(item, "ItemNumber", str(number))
            _el(item, "Description", f"Item {number}")
            _el(item, "DamageCode", damage)
            _el(item, "Quantity", "2.5")
        text = _el(data, "LongText")
        _el(text, "TextLine", "Linha 1")
        _el(text, "TextLine", "Linha 2")
        attrs = _el(data, "Attributes")
        attr = _el(attrs, "Attribute")
        _el(attr, "Name", "area")
        _el(attr, "Value", "norte")

    def _fill_created(
        self, data: etree._Element, notification_id: str, stored: dict[str, Any]
    ) -> None:
        _el(data, "NotificationId", notification_id)
        header = self._header(data, description=stored["description"])
        _el(header, "Plant", stored["plant"])
        _el(data, "Status", "OSNO")
        _el(data, "CreatedOn", stored["created"].date().isoformat())
        _el(data, "CreatedAt", stored["created"].isoformat().replace("+00:00", "Z"))

    async def _create(self, node: etree._Element) -> Response:
        header = node.find(_q("Header"))
        description = (_text(header, "Description") if header is not None else None) or ""
        plant = (_text(header, "Plant") if header is not None else None) or ""
        if description == "FAIL_INVALID":
            return _fault("INVALID_INPUT", "invalid description")
        if description == "FAIL_HTTP_503":
            return Response("unavailable", status_code=503, media_type="text/plain")
        if description == "FAIL_SLOW":
            await asyncio.sleep(self.slow_seconds)
        notification_id = f"{next(self._ids):012d}"
        with self._lock:
            self._created[notification_id] = {
                "description": description,
                "plant": plant,
                "created": datetime.now(UTC).replace(microsecond=0),
            }
        response = etree.Element(_q("NotificationCreateResponse"))
        _el(response, "NotificationId", notification_id)
        messages = _el(response, "Messages")
        message = _el(messages, "Message")
        _el(message, "Severity", "S")
        _el(message, "Text", "Nota criada com sucesso")
        return Response(_envelope(response), media_type="text/xml; charset=utf-8")

    async def _search(self, node: etree._Element) -> Response:
        plant = _text(node, "Plant")
        status = _text(node, "Status")
        max_results = int(_text(node, "MaxResults") or "50")
        response = etree.Element(_q("NotificationSearchResponse"))
        if plant == PLANT_NO_COLLECTION:
            _el(response, "Truncated", "false")
            return Response(_envelope(response), media_type="text/xml; charset=utf-8")
        rows: list[tuple[str, str, str, str | None, date]] = []
        if plant != PLANT_EMPTY_COLLECTION:
            rows.append((ID_SAMPLE_FULL, "Bomba P-101 vazando", "NOPR", "2", date(2026, 3, 1)))
            rows.append((ID_SAMPLE_TRISTATE, "", "OSNO", None, date(2026, 3, 1)))
            with self._lock:
                for nid, stored in self._created.items():
                    rows.append(
                        (nid, stored["description"], "OSNO", None, stored["created"].date())
                    )
            if status:
                rows = [row for row in rows if row[2] == status]
        notifications = _el(response, "Notifications")
        for nid, description, row_status, priority, created in rows[:max_results]:
            summary = _el(notifications, "Notification")
            _el(summary, "NotificationId", nid)
            _el(summary, "Description", description)
            _el(summary, "Status", row_status)
            if priority:
                _el(summary, "Priority", priority)
            _el(summary, "CreatedOn", created.isoformat())
        _el(response, "Truncated", "true" if len(rows) > max_results else "false")
        return Response(_envelope(response), media_type="text/xml; charset=utf-8")


class MockSapServer:
    """Executa o mock em uma thread, numa porta livre — usado pelos testes de integração."""

    def __init__(self, users: dict[str, str], *, slow_seconds: float = 5.0) -> None:
        self.mock = MockSapApp(users, slow_seconds=slow_seconds)
        config = uvicorn.Config(
            self.mock.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}{SOAP_PATH}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("mock SAP não iniciou")
            time.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self._server.should_exit = True
        with contextlib.suppress(RuntimeError):
            self._thread.join(timeout=5)


def run() -> None:
    cfg = MockSapSettings()
    users = cfg.parsed_users()
    if not users:
        raise SystemExit(
            "Defina MOCK_SAP_USERS='usuario:senha,outro:senha' (apenas para demonstração)."
        )
    app = MockSapApp(users, slow_seconds=cfg.slow_seconds)
    uvicorn.run(app.app, host=cfg.host, port=cfg.port, log_level="info")
