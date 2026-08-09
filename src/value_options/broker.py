"""Strictly read-only Alpaca boundary; deliberately incapable of trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Any, Mapping, Protocol

from .market_data import canonical_json
from .models import require_utc


@dataclass(frozen=True)
class ProviderResponse:
    endpoint: str
    request_id: str
    feed: str
    provider_timestamp: datetime
    received_at: datetime
    raw: Any
    raw_sha256: str

    @classmethod
    def capture(cls, endpoint: str, request_id: str, feed: str,
                provider_timestamp: datetime, received_at: datetime, raw: Any):
        require_utc(provider_timestamp, "provider_timestamp")
        require_utc(received_at, "received_at")
        if not request_id or not feed: raise ValueError("request ID and feed identity are required")
        if received_at < provider_timestamp: raise ValueError("response received before provider timestamp")
        return cls(endpoint, request_id, feed, provider_timestamp, received_at, raw,
                   hashlib.sha256(canonical_json(raw)).hexdigest())


class ReadOnlyAlpaca(Protocol):
    """The complete port: no orders, account state, cancel, or replace surface."""
    def clock(self) -> ProviderResponse: ...
    def calendar(self, start: str, end: str) -> ProviderResponse: ...
    def underlying_quote(self, symbol: str) -> ProviderResponse: ...
    def option_chain(self, underlying: str) -> ProviderResponse: ...
    def option_quote(self, symbol: str) -> ProviderResponse: ...


class FixtureAlpaca:
    """Offline CI adapter. Values must be injected fixtures, never network data."""
    _ENDPOINTS = frozenset({"clock", "calendar", "underlying_quote", "option_chain", "option_quote"})

    def __init__(self, fixtures: Mapping[str, ProviderResponse]):
        unknown = set(fixtures) - self._ENDPOINTS
        if unknown: raise ValueError(f"write-side or unknown Alpaca operation: {sorted(unknown)}")
        self._fixtures = dict(fixtures)

    def _get(self, name: str) -> ProviderResponse:
        try: return self._fixtures[name]
        except KeyError as error: raise ValueError(f"missing offline fixture: {name}") from error

    def clock(self): return self._get("clock")
    def calendar(self, start, end): return self._get("calendar")
    def underlying_quote(self, symbol): return self._get("underlying_quote")
    def option_chain(self, underlying): return self._get("option_chain")
    def option_quote(self, symbol): return self._get("option_quote")
