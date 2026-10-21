"""Which weather sources a request may use (free vs paid answers).

Open-Meteo's free API is for non-commercial use only (https://open-meteo.com/en/terms).
Paid answers (paid API-key plans and x402 pay-per-call) must therefore come from
sources that allow commercial use:

* US points: National Weather Service / NOAA only (api.weather.gov, open data).
* Outside the US: MET Norway Locationforecast (api.met.no, CC BY 4.0 + NLOD 2.0).

The switch is ``RAINOUT_COMMERCIAL_SOURCES`` and it defaults to ``off`` so nothing
changes until Jimmy turns it on:

* ``off``  (default) every request uses today's sources (NWS + Open-Meteo).
* ``paid`` requests marked paid (``paid_request()``) use only NWS (US) / MET Norway (non-US).
* ``all``  every request uses the commercial-safe sources (see docs/data-sources.md for why
           this may be needed once paid plans exist at all).

Code marks a request as paid with ``with paid_request(): ...``. The mark is a context
variable, so it follows the request but not other requests on the same instance. Threads
started inside must copy the context (``contextvars.copy_context().run``).
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Iterator

MODES = ("off", "paid", "all")
ENV = "RAINOUT_COMMERCIAL_SOURCES"
OPEN_METEO_HOSTS = ("api.open-meteo.com", "geocoding-api.open-meteo.com", "archive-api.open-meteo.com",
                    "historical-forecast-api.open-meteo.com", "ensemble-api.open-meteo.com")

_PAID: contextvars.ContextVar[bool] = contextvars.ContextVar("rainout_paid_request", default=False)


class SourceNotAllowed(RuntimeError):
    """A non-commercial source was about to be called while answering a paid request."""


def mode(env: dict[str, str] | None = None) -> str:
    value = ((env if env is not None else os.environ).get(ENV) or "off").strip().lower()
    return value if value in MODES else "off"


def is_paid_request() -> bool:
    return _PAID.get()


@contextlib.contextmanager
def paid_request(paid: bool = True) -> Iterator[None]:
    token = _PAID.set(bool(paid))
    try:
        yield
    finally:
        _PAID.reset(token)


def commercial_only(env: dict[str, str] | None = None) -> bool:
    """True when this request may only use commercial-safe sources (NWS/NOAA, MET Norway)."""
    m = mode(env)
    return m == "all" or (m == "paid" and is_paid_request())


def tier_label() -> str:
    return "commercial" if commercial_only() else "free"


def check_url(url: str) -> None:
    """Raise SourceNotAllowed if ``url`` is an Open-Meteo endpoint and this request is commercial-only."""
    if not commercial_only():
        return
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if host in OPEN_METEO_HOSTS or host.endswith(".open-meteo.com"):
        raise SourceNotAllowed("Open-Meteo's free API is non-commercial; paid answers use NWS (US) or MET Norway.")
