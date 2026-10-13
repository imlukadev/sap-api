import pytest
from pydantic import ValidationError

from sap_gateway.config import Settings, SoapAuthMode, SoapServiceSettings, TelemetrySettings


def test_defaults_start_without_any_environment():
    settings = Settings(_env_file=None)
    assert settings.environment == "local"
    assert settings.service_name == "sap-gateway"
    assert settings.soap_service("maintenance_notification") == SoapServiceSettings()
    telemetry = settings.telemetry
    assert not telemetry.any_export_enabled
    assert telemetry.otlp_protocol == "grpc"
    assert telemetry.otlp_endpoint == "http://localhost:4317"


def test_soap_services_from_nested_environment_variables(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv(
        "APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__ENDPOINT_URL", "https://sap.example/svc"
    )
    monkeypatch.setenv("APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__READ_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__RETRY__MAX_ATTEMPTS", "4")
    monkeypatch.setenv(
        "APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__AUTH_MODE", "ws_security_username_token"
    )
    monkeypatch.setenv(
        "APP_SOAP_SERVICES__MAINTENANCE_NOTIFICATION__ALLOWED_HOSTS", "a.example, b.example"
    )
    cfg = Settings(_env_file=None).soap_service("maintenance_notification")
    assert cfg.endpoint_url == "https://sap.example/svc"
    assert cfg.read_timeout_seconds == 12.5
    assert cfg.retry.max_attempts == 4
    assert cfg.auth_mode is SoapAuthMode.WS_SECURITY_USERNAME_TOKEN


def test_standard_otel_variables_are_honoured(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-gateway")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://alloy:4318/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1500")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer topsecret")
    monkeypatch.setenv("APP_OTEL_TRACES_ENABLED", "true")
    monkeypatch.setenv("APP_OTEL_HTTP_EXCLUDED_URLS", "/a, /b")
    settings = Settings(_env_file=None)
    assert settings.service_name == "my-gateway"
    telemetry = settings.telemetry
    assert telemetry.otlp_endpoint == "http://alloy:4318"
    assert telemetry.otlp_protocol == "http/protobuf"
    assert telemetry.otlp_timeout_ms == 1500
    assert telemetry.traces_enabled and not telemetry.logs_enabled
    assert telemetry.http_excluded_urls == ["/a", "/b"]
    assert "topsecret" not in repr(telemetry)
    assert "topsecret" not in repr(settings)


def test_wsdl_password_is_secret():
    cfg = SoapServiceSettings(wsdl_username="svc", wsdl_password="hunter2")  # type: ignore[arg-type]
    assert "hunter2" not in repr(cfg)
    assert cfg.wsdl_password is not None and cfg.wsdl_password.get_secret_value() == "hunter2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"endpoint_url": "ftp://sap.example/x"},
        {"endpoint_url": "https://user:pw@sap.example/x"},
        {"wsdl": "https://user:pw@sap.example/x?wsdl"},
        {"read_timeout_seconds": 0},
        {"retry": {"max_attempts": 0}},
    ],
)
def test_invalid_service_settings_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        SoapServiceSettings(**kwargs)


def test_invalid_otlp_endpoint_scheme_is_rejected(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "alloy:4317")
    with pytest.raises(ValidationError):
        TelemetrySettings(_env_file=None)


def test_invalid_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "prod-ish")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
