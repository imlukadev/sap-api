import pickle

import pytest
import requests
from fastapi.security import HTTPBasicCredentials

from sap_gateway.auth import BasicCredentials, get_credentials
from sap_gateway.config import SoapAuthMode, SoapServiceSettings, TlsSettings
from sap_gateway.errors import AuthenticationRequiredError
from sap_gateway.soap.transport import RequestTransport, build_session

CREDS = BasicCredentials("alice", "Sup3r-S3cret")


def test_credentials_repr_and_pickle_do_not_leak_password():
    assert "Sup3r-S3cret" not in repr(CREDS)
    assert "Sup3r-S3cret" not in str(CREDS)
    with pytest.raises(TypeError):
        pickle.dumps(CREDS)


def test_each_session_is_new_and_configured_per_auth_mode():
    cfg = SoapServiceSettings()
    first = build_session(cfg, CREDS)
    second = build_session(cfg, BasicCredentials("bob", "other"))
    assert first is not second
    assert first.auth.username == "alice" and second.auth.username == "bob"
    assert first.trust_env is False
    assert not first.cookies
    ws = build_session(
        SoapServiceSettings(auth_mode=SoapAuthMode.WS_SECURITY_USERNAME_TOKEN), CREDS
    )
    assert ws.auth is None
    assert build_session(SoapServiceSettings(auth_mode=SoapAuthMode.NONE), CREDS).auth is None
    assert build_session(cfg).auth is None


def test_session_tls_and_proxy_settings(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    session = build_session(
        SoapServiceSettings(tls=TlsSettings(ca_bundle=ca), proxy_url="http://proxy:3128"), CREDS
    )
    assert session.verify == str(ca)
    assert session.proxies["https"] == "http://proxy:3128"
    assert build_session(SoapServiceSettings(tls=TlsSettings(verify=False))).verify is False


def test_request_transport_applies_explicit_timeouts():
    transport = RequestTransport(requests.Session(), timeouts=(1, 2))
    assert transport.operation_timeout == (1, 2)


@pytest.mark.parametrize(
    ("username", "password"),
    [("", "x"), ("x", ""), ("a" * 129, "x"), ("x", "p" * 257), ("us\ner", "x"), ("x", "p\x00w")],
)
def test_malformed_credentials_are_rejected(username, password):
    with pytest.raises(AuthenticationRequiredError):
        get_credentials(HTTPBasicCredentials(username=username, password=password))


def test_missing_credentials_are_rejected():
    with pytest.raises(AuthenticationRequiredError):
        get_credentials(None)
