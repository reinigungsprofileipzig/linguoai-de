"""Abbruch-, Retry- und Secret-Redaction-Helfer für Provider-Adapter."""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import quote, quote_plus

from .errors import CancelledError

T = TypeVar("T")


def check_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise CancelledError("Verarbeitung wurde abgebrochen.")


def retry_call(
    function: Callable[[], T],
    *,
    attempts: int,
    cancel_event: threading.Event,
    on_retry: Callable[[int, BaseException], None] | None = None,
    should_retry: Callable[[BaseException], bool] | None = None,
    base_delay_seconds: float = 0.75,
    max_delay_seconds: float = 8.0,
) -> T:
    """Wiederholt nur vollständig beendete Aufrufe.

    Provider müssen ihren nativen Netzwerk-Timeout konfigurieren. Dadurch gibt es
    keine verwaisten Daemon-Threads und keine überlappenden, kostenpflichtigen Calls.
    """
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        check_cancelled(cancel_event)
        try:
            return function()
        except CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            retryable = should_retry(exc) if should_retry else True
            if attempt >= attempts or not retryable:
                break
            if on_retry:
                on_retry(attempt, exc)
            delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            if delay > 0:
                delay += random.uniform(0.0, min(0.5, delay * 0.1))
            if cancel_event.wait(delay):
                raise CancelledError("Verarbeitung wurde abgebrochen.") from None
    assert last_error is not None
    raise last_error


def redact_secrets(value: object, *secrets: str) -> str:
    """Entfernt bekannte Schlüssel auch in URL-kodierter Form aus Fehlermeldungen."""
    text = str(value)
    for secret in secrets:
        if not secret:
            continue
        variants = {secret, quote(secret, safe=""), quote_plus(secret)}
        for variant in variants:
            text = text.replace(variant, "[REDACTED]")
    return text
