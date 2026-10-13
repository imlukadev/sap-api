import pickle

import pytest
import requests

from sap_gateway.auth import BasicCredentials, get_credentials
from sap_gateway.config import SoapAuthMode, SoapServiceSettings, TlsSettings
from sap_gateway.errors import AuthenticationRequiredError, SoapWsdlUnavailableError
from sap_gateway.services.maintenance_notification.contracts import SERVICE
from sap_gateway.soap.transport import (
    LoadTransport,
    LocationNotAllowedError,
    RequestTransport,
    build_request_session,
)
from sap_gateway.soap.wsdl import WsdlStore

CREDS = BasicCredentials("alice", "Sup3r-S3cret")


def test_credentials_repr_and_pickle_do_not_leak_password():
    assert "Sup3r-S3cret" not in repr(CREDS)
    assert "Sup3r-S3cret" not in str(CREDS)
    assert "Sup3r-S3cret" not in f"{CREDS!r:>80}"
    with pytest.raises(TypeError):
        pickle.dumps(CREDS)


def test_each_session_is_new_and_configured_per_auth_mode():
    cfg = SoapServiceSettings()
    first = build_request_session(cfg, CREDS)
    second = build_request_session(cfg, BasicCredentials("bob", "other"))
    assert first is not second
    assert first.auth == ("alice", "Sup3r-S3cret") or first.auth.username == "alice"
    assert second.auth.username == "bob"
    assert first.trust_env is False and not first.cookies
    ws = build_request_session(
        SoapServiceSettings(auth_mode=SoapAuthMode.WS_SECURITY_USERNAME_TOKEN), CREDS
    )
    assert ws.auth is None
    none = build_request_session(SoapServiceSettings(auth_mode=SoapAuthMode.NONE), CREDS)
    assert none.auth is None


def test_session_tls_and_proxy_settings(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    session = build_request_session(
        SoapServiceSettings(tls=TlsSettings(ca_bundle=ca), proxy_url="http://proxy:3128"), CREDS
    )
    assert session.verify == str(ca)
    assert session.proxies["https"] == "http://proxy:3128"
    assert (
        build_request_session(SoapServiceSettings(tls=TlsSettings(verify=False)), CREDS).verify
        is False
    )


def test_request_transport_only_posts_to_the_configured_address():
    session = requests.Session()
    transport = RequestTransport(
        session, address="https://sap.example/svc", connect_timeout=1, read_timeout=2
    )
    assert transport.operation_timeout == (1, 2)
    with pytest.raises(LocationNotAllowedError):
        transport.post("https://evil.example/", b"", {})
    with pytest.raises(LocationNotAllowedError):
        transport.get("https://sap.example/svc?wsdl", None, None)
    with pytest.raises(LocationNotAllowedError):
        transport.load("https://sap.example/svc?wsdl")


def test_load_transport_blocks_unlisted_hosts_schemes_and_paths(tmp_path):
    inside = tmp_path / "a.xsd"
    inside.write_text("<x/>")
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nope")
    transport = LoadTransport(
        SoapServiceSettings(), allowed_hosts=frozenset({"sap.example"}), root=tmp_path
    )
    assert transport.load(str(inside)) == b"<x/>"
    for bad in [
        "http://169.254.169.254/latest/meta-data",
        "https://internal.corp/x.xsd",
        "ftp://sap.example/x",
        str(outside),
        str(tmp_path / ".." / "secret.txt"),
        "file:///etc/passwd",
        "gopher://sap.example/",
    ]:
        with pytest.raises(LocationNotAllowedError):
            transport.load(bad)


def test_wsdl_store_maps_load_failures_to_503_and_does_not_cache_them(tmp_path):
    store = WsdlStore()
    cfg = SoapServiceSettings(wsdl=str(tmp_path / "missing.wsdl"))
    with pytest.raises(SoapWsdlUnavailableError):
        store.get(SERVICE, cfg)
    assert not store.is_loaded(SERVICE.key)
    ok = store.get(SERVICE, SoapServiceSettings())
    assert store.is_loaded(SERVICE.key) and store.get(SERVICE, SoapServiceSettings()) is ok


def test_wsdl_with_import_outside_its_directory_is_rejected(tmp_path):
    (tmp_path / "w.wsdl").write_text(
        '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:t">'
        '<wsdl:types><xsd:schema><xsd:import namespace="urn:x" schemaLocation="../outside.xsd"/>'
        "</xsd:schema></wsdl:types></wsdl:definitions>"
    )
    (tmp_path.parent / "outside.xsd").write_text(
        '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:x"/>'
    )
    with pytest.raises(SoapWsdlUnavailableError):
        WsdlStore().get(SERVICE, SoapServiceSettings(wsdl=str(tmp_path / "w.wsdl")))


@pytest.mark.parametrize(
    ("username", "password"),
    [("", "x"), ("x", ""), ("a" * 129, "x"), ("x", "p" * 257), ("us\ner", "x"), ("x", "p\x00w")],
)
def test_malformed_credentials_are_rejected(username, password):
    from fastapi.security import HTTPBasicCredentials

    with pytest.raises(AuthenticationRequiredError):
        get_credentials(HTTPBasicCredentials(username=username, password=password))


def test_missing_credentials_are_rejected():
    with pytest.raises(AuthenticationRequiredError):
        get_credentials(None)
