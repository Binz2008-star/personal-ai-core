"""What kind of failure a provider's HTTP transport met, for ProviderError.kind."""
from __future__ import annotations

import urllib.error
from typing import Callable, TypeVar

from ..core.errors import ProviderError

T = TypeVar("T")

# One retry, after this long, when nothing answered (gap analysis P1-7).
RETRY_DELAY_SECONDS = 2.0


def transport_failure(exc: BaseException) -> tuple[str, int | None]:
    """(kind, HTTP status) for an exception raised by urlopen or a read.

    A status is a number and a kind is one word, so both are safe to keep in
    a durable record; the exception's text is not (it can name the host).
    """
    if isinstance(exc, urllib.error.HTTPError):
        return "http_status", exc.code
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout", None
    return "unreachable", None


def with_one_retry(call: Callable[[], T], *, sleep: Callable[[float], None],
                   delay: float = RETRY_DELAY_SECONDS) -> T:
    """`call()`, and once more after `delay` if nothing answered the first time.

    Gap analysis P1-7: one refused or reset connection -- Ollama restarting,
    the machine waking -- failed the turn. Only `unreachable` is retried: a
    timeout already waited the whole limit and a second would double it, and
    a malformed response or a missing model will not change in two seconds.
    """
    try:
        return call()
    except ProviderError as exc:
        if exc.kind != "unreachable":
            raise
    # Outside the handler, so a second failure does not carry the first.
    sleep(delay)
    return call()

