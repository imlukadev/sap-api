"""Instrumentos de métricas próprios da aplicação.

Métricas HTTP (contagem, latência, erros por rota) vêm da instrumentação oficial do FastAPI
(``http.server.request.duration``) e não são duplicadas aqui. Dimensões permitidas: serviço SOAP,
operação SOAP e categoria de erro — todas de cardinalidade limitada. Usuário, IDs de nota e
quaisquer identificadores de requisição são proibidos como atributos de métrica.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from opentelemetry.metrics import CallbackOptions, Meter, Observation

SOAP_DURATION_BUCKETS = [0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]

ALLOWED_SOAP_ATTRIBUTES = frozenset({"soap.service", "soap.operation", "error.type"})


class SoapMetrics:
    def __init__(self, meter: Meter) -> None:
        self.calls = meter.create_counter(
            "soap.client.calls",
            unit="{call}",
            description="Chamadas SOAP lógicas (já incluindo retries), por resultado final.",
        )
        self.duration = meter.create_histogram(
            "soap.client.duration",
            unit="s",
            description="Duração de uma chamada SOAP lógica, incluindo retries e esperas.",
            explicit_bucket_boundaries_advisory=SOAP_DURATION_BUCKETS,
        )
        self.timeouts = meter.create_counter(
            "soap.client.timeouts",
            unit="{timeout}",
            description="Tentativas SOAP que estouraram o timeout de conexão ou leitura.",
        )
        self.retries = meter.create_counter(
            "soap.client.retries",
            unit="{retry}",
            description="Novas tentativas realizadas após falha transitória.",
        )

    def record_call(
        self, service: str, operation: str, seconds: float, error_type: str | None
    ) -> None:
        attrs = _attrs(service, operation, error_type)
        self.calls.add(1, attrs)
        self.duration.record(seconds, attrs)

    def record_timeout(self, service: str, operation: str) -> None:
        self.timeouts.add(1, _attrs(service, operation, None))

    def record_retry(self, service: str, operation: str, error_type: str) -> None:
        self.retries.add(1, _attrs(service, operation, error_type))


def _attrs(service: str, operation: str, error_type: str | None) -> dict[str, str]:
    attrs = {"soap.service": service, "soap.operation": operation}
    if error_type is not None:
        attrs["error.type"] = error_type
    return attrs


def register_health_gauge(meter: Meter, is_ready: Callable[[], bool]) -> None:
    def _observe(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(1 if is_ready() else 0)

    meter.create_observable_gauge(
        "app.health.status",
        callbacks=[_observe],
        unit="{status}",
        description="1 quando a aplicação está pronta para atender requisições, 0 caso contrário.",
    )
