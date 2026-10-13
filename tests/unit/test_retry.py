import random

import pytest

from sap_gateway.config import RetrySettings
from sap_gateway.errors import AuthenticationFailedError
from sap_gateway.soap.errors import (
    SoapConnectionError,
    SoapFaultError,
    SoapHttpError,
    SoapResponseError,
    SoapTimeoutError,
)
from sap_gateway.soap.retry import backoff_delay, is_retryable


@pytest.mark.parametrize(
    ("error", "idempotent", "expected"),
    [
        (SoapConnectionError(request_sent=False), True, True),
        (SoapConnectionError(request_sent=False), False, True),
        (SoapConnectionError(request_sent=True), True, True),
        (SoapConnectionError(request_sent=True), False, False),
        (SoapConnectionError(request_sent=False, transient=False), True, False),
        (SoapTimeoutError(request_sent=False), False, True),
        (SoapTimeoutError(request_sent=True), True, True),
        (SoapTimeoutError(request_sent=True), False, False),
        (SoapHttpError(upstream_status=503), True, True),
        (SoapHttpError(upstream_status=503), False, False),
        (SoapHttpError(upstream_status=500), True, False),
        (SoapHttpError(upstream_status=404), True, False),
        (SoapFaultError(fault_code="x", fault_message="y"), True, False),
        (AuthenticationFailedError(), True, False),
        (SoapResponseError(), True, False),
    ],
)
def test_retry_matrix(error, idempotent, expected):
    assert is_retryable(error, idempotent=idempotent) is expected


def test_backoff_is_exponential_capped_and_jittered():
    policy = RetrySettings(max_attempts=6, backoff_base_seconds=0.1, backoff_max_seconds=0.5)

    class Top:
        def uniform(self, low, high):
            return high

    class Bottom:
        def uniform(self, low, high):
            return low

    ceilings = [backoff_delay(n, policy, Top()) for n in range(1, 6)]
    assert ceilings == pytest.approx([0.1, 0.2, 0.4, 0.5, 0.5])
    assert all(backoff_delay(n, policy, Bottom()) == 0 for n in range(1, 6))
    rng = random.Random(1)  # noqa: S311
    samples = [backoff_delay(3, policy, rng) for _ in range(200)]
    assert all(0 <= s <= 0.4 for s in samples) and len(set(samples)) > 100
