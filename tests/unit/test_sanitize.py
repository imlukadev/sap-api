import base64
import logging

import pytest

from sap_gateway.observability.logging import ExcludeLoggersFilter, SanitizingFilter
from sap_gateway.observability.sanitize import REDACTED, redact_text, sanitize_value


def _basic(user: str, password: str) -> str:
    return base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (f"Authorization: Basic {_basic('alice', 'pw123456')}", _basic("alice", "pw123456")),
        (f"got header authorization=Basic {_basic('a', 'b')} here", _basic("a", "b")),
        ("sent Bearer abcdefghijklmnop.qrstuv", "abcdefghijklmnop"),
        ('{"password": "Tr0ub4dor&3"}', "Tr0ub4dor&3"),
        ("password=hunter2&user=alice", "hunter2"),
        ("<wsse:Password Type='x'>topsecret</wsse:Password>", "topsecret"),
        ("<Password>topsecret</Password>", "topsecret"),
        ("Set-Cookie: SAP_SESSIONID=abc123; Path=/", "abc123"),
        ("Cookie: MYSAPSSO2=zzz999", "zzz999"),
        ("GET http://alice:pw-in-url@sap.example/x", "pw-in-url"),
        ("api_key: sk-123456", "sk-123456"),
    ],
)
def test_redact_text_removes_secret(text, secret):
    redacted = redact_text(text)
    assert secret not in redacted
    assert REDACTED in redacted


def test_redact_text_keeps_innocuous_text_and_truncates():
    assert redact_text("soap call completed in 12ms") == "soap call completed in 12ms"
    long = redact_text("x" * 5000, limit=100)
    assert len(long) < 130 and long.endswith("[truncated]")


@pytest.mark.parametrize(
    "key", ["password", "Authorization", "set-cookie", "api_key", "session_id", "x-token"]
)
def test_sensitive_keys_are_redacted_regardless_of_value(key):
    assert sanitize_value(key, "anything") == REDACTED


def test_non_sensitive_primitives_pass_through():
    assert sanitize_value("duration_ms", 12.5) == 12.5
    assert sanitize_value("soap.service", "svc") == "svc"
    assert sanitize_value("obj", object).startswith("<class")


def _record(msg="m", args=(), exc_info=None, **extra):
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, exc_info)
    record.__dict__.update(extra)
    return record


def test_sanitizing_filter_redacts_message_args_extras_and_exceptions():
    try:
        raise RuntimeError("failed with password=hunter2")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    record = _record(
        "login %s",
        ("Authorization: Basic QWxhZGRpbjpvcGVu",),
        exc_info,
        password="x",
        username="alice",
    )
    assert SanitizingFilter().filter(record)
    assert "QWxhZGRpbjpvcGVu" not in record.getMessage()
    assert record.__dict__["password"] == REDACTED
    assert record.__dict__["username"] == "alice"
    assert record.exc_info is None
    assert record.__dict__["exception.type"] == "RuntimeError"
    assert "hunter2" not in record.__dict__["exception.message"]
    assert "hunter2" not in record.__dict__["exception.stacktrace"]


def test_sanitizing_filter_limits_number_of_extras():
    record = _record(**{f"k{i}": i for i in range(100)})
    SanitizingFilter().filter(record)
    extras = [k for k in record.__dict__ if k.startswith("k") and k[1:].isdigit()]
    assert len(extras) <= 32


def test_sanitizing_filter_is_idempotent():
    record = _record("hello %s", ("world",))
    flt = SanitizingFilter()
    flt.filter(record)
    flt.filter(record)
    assert record.getMessage() == "hello world"


def test_exclude_loggers_filter_blocks_exporter_feedback_loop():
    flt = ExcludeLoggersFilter(("opentelemetry", "grpc"))
    assert not flt.filter(
        logging.LogRecord("opentelemetry.exporter.otlp", 30, "", 0, "x", (), None)
    )
    assert flt.filter(logging.LogRecord("sap_gateway.soap", 30, "", 0, "x", (), None))
