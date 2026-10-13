"""Retry apenas de falhas transitórias em operações seguras, com backoff exponencial e jitter."""

from __future__ import annotations

import random

from sap_gateway.config import RetrySettings
from sap_gateway.errors import AppError, SoapConnectionError, SoapTimeoutError, SoapTransportError

RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


def is_retryable(error: AppError, *, idempotent: bool) -> bool:
    """Decide se repetir é seguro.

    * Falha **antes** do envio (conexão recusada, DNS, timeout de conexão): sempre seguro.
    * Falha **depois** do envio (timeout de leitura, 5xx): só se a operação for idempotente.
    * SOAP Fault, 4xx, autenticação, contrato: nunca (determinísticos).
    """
    if isinstance(error, SoapConnectionError):
        return error.transient and (idempotent or not error.request_sent)
    if isinstance(error, SoapTimeoutError):
        return idempotent or not error.request_sent
    if isinstance(error, SoapTransportError):
        return idempotent and error.upstream_status in RETRYABLE_HTTP_STATUS
    return False


def backoff_delay(attempt: int, policy: RetrySettings, rng: random.Random) -> float:
    """Full jitter: ``U(0, min(max, base * 2**(attempt-1)))``, com ``attempt`` iniciando em 1."""
    ceiling = min(policy.backoff_max_seconds, policy.backoff_base_seconds * 2 ** (attempt - 1))
    return rng.uniform(0, ceiling)
