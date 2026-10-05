"""What kind of failure a provider's HTTP transport met, for ProviderError.kind."""
from __future__ import annotations

import urllib.error


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
