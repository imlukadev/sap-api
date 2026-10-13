"""Utilitários de teste (sem fixtures): construção de Settings hermético e coleta de telemetria."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sap_gateway.config import PACKAGED_WSDL, Settings, SoapServiceSettings, TelemetrySettings
from sap_gateway.observability.telemetry import Telemetry
from sap_gateway.services.maintenance_notification import contracts as notification_contracts
from sap_gateway.soap.service import SoapService

SERVICE_KEY = "maintenance_notification"

USERS = {"alice": "Alice-S3cret-PW!", "bob": "Bob-Pa55word#"}


def build_settings(
    endpoint: str | None = None,
    *,
    environment: str = "test",
    telemetry: TelemetrySettings | None = None,
    **service_overrides: Any,
) -> Settings:
    service_args: dict[str, Any] = {
        "endpoint_url": endpoint,
        "connect_timeout_seconds": 2.0,
        "read_timeout_seconds": 5.0,
        **service_overrides,
    }
    return Settings(
        _env_file=None,
        environment=environment,
        soap_services={SERVICE_KEY: SoapServiceSettings(**service_args)},
        telemetry=telemetry or TelemetrySettings(_env_file=None),
    )


@dataclass
class Collected:
    telemetry: Telemetry
    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader
    logs: InMemoryLogRecordExporter
    stream: io.StringIO

    def json_logs(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line.strip()]

    def metric_points(self) -> dict[str, list[Any]]:
        """Nome da métrica → data points (coletado agora)."""
        data = self.metrics.get_metrics_data()
        out: dict[str, list[Any]] = {}
        if data is None:
            return out
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    out.setdefault(metric.name, []).extend(metric.data.data_points)
        return out


def build_collected(settings: Settings) -> Collected:
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    logs = InMemoryLogRecordExporter()
    stream = io.StringIO()
    telemetry = Telemetry(
        settings, span_exporter=spans, metric_reader=metrics, log_exporter=logs, log_stream=stream
    )
    return Collected(telemetry, spans, metrics, logs, stream)


def build_soap(
    settings: Settings, collected: Collected, *, sleeps: list[float] | None = None
) -> SoapService:
    recorder = sleeps if sleeps is not None else []
    return SoapService(
        notification_contracts.SERVICE_KEY,
        notification_contracts.BINDING,
        settings.soap_service(notification_contracts.SERVICE_KEY),
        default_wsdl=PACKAGED_WSDL / "maintenance_notification.wsdl",
        telemetry=collected.telemetry,
        require_https=settings.environment not in {"local", "test"},
        sleep=recorder.append,
        rng=_ConstantRandom(),  # type: ignore[arg-type]
    )


class _ConstantRandom:
    """Jitter determinístico: sempre o teto do intervalo."""

    def uniform(self, low: float, high: float) -> float:
        return high


def build_soap_for(settings: Settings, telemetry: Telemetry) -> SoapService:
    """SoapService de produção ligado a um Telemetry já existente (testes OTLP/e2e)."""
    return SoapService(
        notification_contracts.SERVICE_KEY,
        notification_contracts.BINDING,
        settings.soap_service(notification_contracts.SERVICE_KEY),
        default_wsdl=PACKAGED_WSDL / "maintenance_notification.wsdl",
        telemetry=telemetry,
        require_https=settings.environment not in {"local", "test"},
    )
