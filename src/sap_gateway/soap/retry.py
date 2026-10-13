"""Retry apenas de falhas transitórias em operações seguras, com backoff exponencial e jitter."""

from __future__ import annotations

import random

from sap_gateway.config import RetrySettings
from sap_gateway.errors import AppError
from sap_gateway.soap.errors import SoapConnectionError, SoapHttpError, SoapTimeoutError

RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


def is_retryable(error: AppError, *, idempotent: bool) -> bool:
    """Falha *antes* do envio (recusa, DNS, timeout de conexão): sempre segura.
    Falha *depois* do envio (timeout de leitura, 502/503/504): só se idempotente.
    SOAP Fault, 4xx, autenticação e resposta inválida: nunca (determinísticos).
    """
    if isinstance(error, SoapConnectionError):
        return error.transient and (idempotent or not error.request_sent)
    if isinstance(error, SoapTimeoutError):
        return idempotent or not error.request_sent
    if isinstance(error, SoapHttpError):
        return idempotent and error.upstream_status in RETRYABLE_HTTP_STATUS
    return False


def backoff_delay(attempt: int, policy: RetrySettings, rng: random.Random) -> float:
    """Full jitter: ``U(0, min(max, base * 2**(attempt-1)))``, ``attempt`` iniciando em 1."""
    ceiling = min(policy.backoff_max_seconds, policy.backoff_base_seconds * 2 ** (attempt - 1))
    return rng.uniform(0, ceiling)
