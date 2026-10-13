"""Configuração centralizada, lida de variáveis de ambiente (e ``.env`` em desenvolvimento).

Três grupos, cada um com seu prefixo:

* ``APP_*``          – aplicação e serviços SOAP (``Settings``);
* ``OTEL_*``         – variáveis padrão do OpenTelemetry (``TelemetrySettings``);
* ``APP_OTEL_*``     – chaves específicas desta aplicação para telemetria.

Nenhum segredo possui valor padrão e todos são ``SecretStr`` para não vazarem em ``repr``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PACKAGED_WSDL = Path(__file__).resolve().parent / "wsdl"

Environment = Literal["local", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class SoapAuthMode(StrEnum):
    """Mecanismo pelo qual as credenciais do usuário chegam ao serviço SOAP."""

    HTTP_BASIC = "http_basic"
    """``Authorization: Basic`` na requisição HTTP (padrão do ICF/SOAMANAGER do SAP)."""

    WS_SECURITY_USERNAME_TOKEN = "ws_security_username_token"  # noqa: S105
    """Cabeçalho SOAP ``wsse:UsernameToken`` (serviços com autenticação de mensagem)."""

    NONE = "none"
    """Sem credenciais (somente serviços públicos / ambientes de teste)."""


class RetrySettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(
        default=3, ge=1, le=6, description="Tentativas totais (1 = sem retry)"
    )
    backoff_base_seconds: float = Field(default=0.2, gt=0)
    backoff_max_seconds: float = Field(default=2.0, gt=0)


class TlsSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    verify: bool = True
    ca_bundle: Path | None = None
    client_cert: Path | None = None
    client_key: Path | None = None


class SoapServiceSettings(BaseModel):
    """Destino SOAP controlado exclusivamente pela configuração (nunca pelo consumidor)."""

    model_config = ConfigDict(frozen=True)

    wsdl: str | None = Field(
        default=None,
        description="Caminho local ou URL http(s) do WSDL. Padrão: WSDL empacotado do serviço.",
    )
    endpoint_url: str | None = Field(
        default=None,
        description="Sobrescreve o soap:address do WSDL (ex.: URL por ambiente).",
    )
    auth_mode: SoapAuthMode = SoapAuthMode.HTTP_BASIC
    strict: bool = Field(
        default=False,
        description="Zeep strict. SAP costuma sair do XSD (ordem, extras): padrão tolerante.",
    )
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)
    wsdl_timeout_seconds: float = Field(default=15.0, gt=0)
    retry: RetrySettings = RetrySettings()
    tls: TlsSettings = TlsSettings()
    proxy_url: str | None = Field(
        default=None, description="Proxy HTTP(S) explícito (variáveis HTTP(S)_PROXY são ignoradas)."
    )
    wsdl_username: str | None = Field(
        default=None,
        description="Credencial técnica, usada SOMENTE para baixar WSDL/XSD protegidos por URL.",
    )
    wsdl_password: SecretStr | None = None

    @field_validator("endpoint_url", "wsdl")
    @classmethod
    def _no_userinfo(cls, value: str | None) -> str | None:
        if value and "@" in (urlparse(value).netloc or ""):
            raise ValueError("URLs não podem conter credenciais embutidas")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _http_scheme(cls, value: str | None) -> str | None:
        if value and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("endpoint_url deve usar http ou https")
        return value


class TelemetrySettings(BaseSettings):
    """Configuração do OpenTelemetry. Variáveis ``OTEL_*`` seguem a especificação oficial."""

    model_config = SettingsConfigDict(
        env_prefix="APP_OTEL_", env_file=".env", extra="ignore", populate_by_name=True
    )

    traces_enabled: bool = False
    metrics_enabled: bool = False
    logs_enabled: bool = False

    otlp_endpoint: str = Field(
        default="http://localhost:4317",
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_ENDPOINT", "otlp_endpoint"),
    )
    otlp_protocol: Literal["grpc", "http/protobuf"] = Field(
        default="grpc",
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_PROTOCOL", "otlp_protocol"),
    )
    otlp_timeout_ms: int = Field(
        default=5000,
        ge=100,
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_TIMEOUT", "otlp_timeout_ms"),
        description="Timeout de cada exportação, em milissegundos (semântica padrão OTEL).",
    )
    otlp_headers: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_HEADERS", "otlp_headers"),
        description="Formato 'k1=v1,k2=v2'. Tratado como segredo.",
    )
    metric_export_interval_ms: int = Field(
        default=15000,
        ge=1000,
        validation_alias=AliasChoices("OTEL_METRIC_EXPORT_INTERVAL", "metric_export_interval_ms"),
    )
    batch_schedule_delay_ms: int = Field(default=2000, ge=100)
    shutdown_timeout_ms: int = Field(default=3000, ge=100)

    http_metrics_enabled: bool = True
    http_excluded_urls: CsvList = Field(
        default_factory=lambda: [
            "/health/live",
            "/health/ready",
            "/docs",
            "/redoc",
            "/openapi.json",
        ]
    )

    @field_validator("http_excluded_urls", mode="before")
    @classmethod
    def _csv_urls(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("otlp_endpoint")
    @classmethod
    def _endpoint_scheme(cls, value: str) -> str:
        if urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT deve começar com http:// ou https://")
        return value.rstrip("/")

    @property
    def any_export_enabled(self) -> bool:
        return self.traces_enabled or self.metrics_enabled or self.logs_enabled


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Environment = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"
    service_name: str = Field(
        default="sap-gateway",
        validation_alias=AliasChoices("OTEL_SERVICE_NAME", "APP_SERVICE_NAME"),
    )
    service_version: str = Field(default="", description="Vazio = versão do pacote.")
    basic_auth_realm: str = "sap-gateway"

    soap_services: dict[str, SoapServiceSettings] = Field(default_factory=dict)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)

    def soap_service(self, key: str) -> SoapServiceSettings:
        return self.soap_services.get(key, SoapServiceSettings())

    def effective_version(self) -> str:
        from sap_gateway import __version__

        return self.service_version or __version__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
