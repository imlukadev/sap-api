"""Inicialização central da observabilidade (traces, métricas e logs via OpenTelemetry).

Um ``Telemetry`` é criado por aplicação, possui seus próprios providers (não altera os globais
do OpenTelemetry) e é encerrado explicitamente com ``shutdown()``. Os exporters OTLP rodam em
threads de fundo com fila limitada e timeout: indisponibilidade do Alloy descarta telemetria,
nunca bloqueia nem derruba requisições de negócio.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import IO

from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as GrpcLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter as GrpcMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as GrpcSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as HttpLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter as HttpMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as HttpSpanExporter,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Tracer

from sap_gateway.config import Settings, TelemetrySettings
from sap_gateway.observability.logging import InstalledLogging, install_logging
from sap_gateway.observability.metrics import SoapMetrics, register_health_gauge

# Semântica HTTP estável (http.route, http.request.method, http.server.request.duration em
# segundos, sem http.target de alta cardinalidade). Precisa estar definido antes da instrumentação.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

logger = logging.getLogger(__name__)

_INSTRUMENTATION_NAME = "sap_gateway"


type MetricExporterT = GrpcMetricExporter | HttpMetricExporter


def _parse_headers(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {key.strip(): value.strip() for key, value in pairs}


def _build_exporters(
    cfg: TelemetrySettings,
) -> tuple[SpanExporter, MetricExporterT, LogRecordExporter]:
    timeout = cfg.otlp_timeout_ms / 1000
    headers = _parse_headers(cfg.otlp_headers.get_secret_value() if cfg.otlp_headers else None)
    if cfg.otlp_protocol == "grpc":
        common: dict[str, object] = {
            "endpoint": cfg.otlp_endpoint,
            "insecure": cfg.otlp_endpoint.startswith("http://"),
            "timeout": timeout,
            "headers": headers,
        }
        return GrpcSpanExporter(**common), GrpcMetricExporter(**common), GrpcLogExporter(**common)  # type: ignore[arg-type]
    base = cfg.otlp_endpoint
    return (
        HttpSpanExporter(endpoint=f"{base}/v1/traces", headers=headers, timeout=timeout),
        HttpMetricExporter(endpoint=f"{base}/v1/metrics", headers=headers, timeout=timeout),
        HttpLogExporter(endpoint=f"{base}/v1/logs", headers=headers, timeout=timeout),
    )


class Telemetry:
    def __init__(
        self,
        settings: Settings,
        *,
        span_exporter: SpanExporter | None = None,
        metric_reader: MetricReader | None = None,
        log_exporter: LogRecordExporter | None = None,
        log_stream: IO[str] | None = None,
    ) -> None:
        cfg = settings.telemetry
        self._cfg = settings.telemetry
        self._lock = threading.Lock()
        self._shutdown = False
        self._ready = False
        self._instrumented_app: FastAPI | None = None
        self._requests_instrumented = False

        self.resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.version": settings.effective_version(),
                "deployment.environment": settings.environment,
                "service.instance.id": str(uuid.uuid4()),
            }
        )

        otlp_span = otlp_metric = otlp_log = None
        if cfg.any_export_enabled:
            otlp_span, otlp_metric, otlp_log = _build_exporters(cfg)
        batch_delay = cfg.batch_schedule_delay_ms
        export_timeout = cfg.otlp_timeout_ms

        self.tracer_provider = TracerProvider(resource=self.resource)
        if span_exporter is not None:
            self.tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif cfg.traces_enabled and otlp_span is not None:
            self.tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    otlp_span,
                    schedule_delay_millis=batch_delay,
                    export_timeout_millis=export_timeout,
                )
            )

        readers: list[MetricReader] = []
        if metric_reader is not None:
            readers.append(metric_reader)
        elif cfg.metrics_enabled and otlp_metric is not None:
            readers.append(
                PeriodicExportingMetricReader(
                    otlp_metric,
                    export_interval_millis=cfg.metric_export_interval_ms,
                    export_timeout_millis=export_timeout,
                )
            )
        self.meter_provider = MeterProvider(resource=self.resource, metric_readers=readers)

        self.logger_provider = LoggerProvider(resource=self.resource)
        otel_handler: LoggingHandler | None = None
        if log_exporter is not None:
            self.logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
            otel_handler = LoggingHandler(logger_provider=self.logger_provider)
        elif cfg.logs_enabled and otlp_log is not None:
            self.logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    otlp_log,
                    schedule_delay_millis=batch_delay,
                    export_timeout_millis=export_timeout,
                )
            )
            otel_handler = LoggingHandler(logger_provider=self.logger_provider)

        self.tracer: Tracer = self.tracer_provider.get_tracer(_INSTRUMENTATION_NAME)
        meter = self.meter_provider.get_meter(_INSTRUMENTATION_NAME)
        self.soap_metrics = SoapMetrics(meter)
        register_health_gauge(meter, lambda: self._ready)

        self._logging: InstalledLogging = install_logging(
            level=settings.log_level,
            service_name=settings.service_name,
            environment=settings.environment,
            otel_handler=otel_handler,
            stream=log_stream,
        )

    def instrument(self, app: FastAPI) -> None:
        """Instrumenta o FastAPI e o transporte ``requests`` usado pelo Zeep."""
        excluded = ",".join(self._cfg.http_excluded_urls)
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            meter_provider=(
                self.meter_provider if self._cfg.http_metrics_enabled else NoOpMeterProvider()
            ),
            excluded_urls=excluded,
            exclude_spans=["receive", "send"],
        )
        self._instrumented_app = app
        RequestsInstrumentor().instrument(
            tracer_provider=self.tracer_provider, meter_provider=self.meter_provider
        )
        self._requests_instrumented = True

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    @property
    def is_ready(self) -> bool:
        return self._ready

    def shutdown(self) -> None:
        """Encerra instrumentação e providers (com flush). Seguro para chamadas repetidas."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._ready = False
        if self._instrumented_app is not None:
            FastAPIInstrumentor.uninstrument_app(self._instrumented_app)
        if self._requests_instrumented:
            RequestsInstrumentor().uninstrument()
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            try:
                provider.shutdown()
            except Exception:
                logger.warning("falha ao encerrar provider de telemetria", exc_info=True)
        self._logging.uninstall()
