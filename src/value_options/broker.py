"""Read-only broker boundary. No implementation, credentials, or order methods."""

from typing import Protocol

from .models import MarketQuote


class ReadOnlyAlpaca(Protocol):
    def latest_quote(self, symbol: str) -> MarketQuote: ...
    def positions(self) -> tuple[dict, ...]: ...
    def account_snapshot(self) -> dict: ...
