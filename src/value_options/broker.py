"""Strictly read-only Alpaca boundary; deliberately incapable of trading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Mapping, Protocol
import json
import os
import re
from urllib.parse import urlencode, urlsplit

from .http import Transport, UrllibTransport

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
    evidence_seal: str = ""
    raw_response: bytes = b""

    @classmethod
    def capture(cls, endpoint: str, request_id: str, feed: str,
                provider_timestamp: datetime, received_at: datetime, raw: Any,
                raw_response: bytes | None = None):
        require_utc(provider_timestamp, "provider_timestamp")
        require_utc(received_at, "received_at")
        if not request_id or not feed: raise ValueError("request ID and feed identity are required")
        if received_at < provider_timestamp: raise ValueError("response received before provider timestamp")
        raw_bytes = canonical_json(raw) if raw_response is None else bytes(raw_response)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        seal = hashlib.sha256(canonical_json({"endpoint": endpoint, "request_id": request_id,
            "feed": feed, "provider_timestamp": provider_timestamp.isoformat(),
            "received_at": received_at.isoformat(), "raw_sha256": digest})).hexdigest()
        return cls(endpoint, request_id, feed, provider_timestamp, received_at, raw,
                   digest, seal, raw_bytes)


class AlpacaReadOnlyClient:
    """Production HTTP adapter with no generic request or broker-write surface."""
    TRADING_HOST = "paper-api.alpaca.markets"
    DATA_HOST = "data.alpaca.markets"
    _FIXED = frozenset({"/v2/clock", "/v2/calendar"})
    _DATA_PATHS = (
        re.compile(r"/v2/stocks/[A-Z0-9.]+/quotes/latest"),
        re.compile(r"/v1beta1/options/snapshots/[A-Z0-9.]+"),
        re.compile(r"/v1beta1/options/quotes/latest"),
    )

    def __init__(self, *, transport: Transport | None = None,
                 environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        self._key, self._secret = env.get("ALPACA_API_KEY_ID", ""), env.get("ALPACA_API_SECRET_KEY", "")
        if not self._key or not self._secret: raise ValueError("Alpaca environment credentials are required")
        self._transport = transport or UrllibTransport()

    def _get(self, host, path, query, endpoint, feed):
        if host not in {self.TRADING_HOST, self.DATA_HOST}: raise ValueError("unapproved Alpaca host")
        approved = (host == self.TRADING_HOST and path in self._FIXED) or (
            host == self.DATA_HOST and any(pattern.fullmatch(path) for pattern in self._DATA_PATHS))
        if not approved or any(x in path.lower() for x in ("order", "account", "cancel", "replace")):
            raise ValueError("unknown or write-side Alpaca endpoint")
        url = f"https://{host}{path}" + ("?" + urlencode(query) if query else "")
        response = self._transport.request("GET", url, headers={"APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret}, body=None)
        final = urlsplit(response.url)
        if final.scheme != "https" or final.hostname != host: raise ValueError("redirect or response from unapproved host")
        if 300 <= response.status < 400: raise ValueError("redirect rejected")
        if response.status != 200: raise ValueError(f"Alpaca read failed with HTTP {response.status}")
        raw = json.loads(response.body)
        now = datetime.now(timezone.utc)
        timestamp = _provider_timestamp(raw, now)
        request_id = response.headers.get("x-request-id") or response.headers.get("X-Request-ID")
        if not request_id: raise ValueError("Alpaca response omitted request ID")
        return ProviderResponse.capture(endpoint, request_id, feed, timestamp, now, raw, response.body)

    def clock(self): return self._get(self.TRADING_HOST, "/v2/clock", {}, "clock", "alpaca-trading")
    def calendar(self, start, end): return self._get(self.TRADING_HOST, "/v2/calendar", {"start": start, "end": end}, "calendar", "alpaca-trading")
    def underlying_quote(self, symbol): return self._get(self.DATA_HOST, f"/v2/stocks/{_symbol(symbol)}/quotes/latest", {"feed": "iex"}, "underlying_quote", "iex")
    def option_chain(self, underlying): return self._get(self.DATA_HOST, f"/v1beta1/options/snapshots/{_symbol(underlying)}", {"feed": "opra"}, "option_chain", "opra")
    def option_quote(self, symbol): return self._get(self.DATA_HOST, "/v1beta1/options/quotes/latest", {"symbols": _symbol(symbol), "feed": "opra"}, "option_quote", "opra")


def _provider_timestamp(value, fallback):
    candidates = [value.get(x) for x in ("timestamp", "t") if isinstance(value, dict)]
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, dict): candidates.extend(child.get(x) for x in ("timestamp", "t") if child.get(x))
    for candidate in candidates:
        if isinstance(candidate, str):
            try: return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError: pass
    return fallback


def _symbol(value):
    if not isinstance(value, str) or not value or not value.replace(".", "").isalnum():
        raise ValueError("invalid symbol")
    return value.upper()


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
