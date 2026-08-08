"""Immutable domain records with explicit event-time semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Any, Mapping
from zoneinfo import ZoneInfo


UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")
DEVELOPED_MARKETS = frozenset({"US", "UK", "CA", "JP", "AU", "NZ", "AT", "BE", "DK", "FI",
                               "FR", "DE", "IE", "IL", "IT", "NL", "NO", "PT", "ES", "SE",
                               "CH", "HK", "SG"})


def require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be timezone-aware UTC")


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetType(str, Enum):
    EQUITY = "equity"
    OPTION = "option"


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_type: AssetType
    issuer: str
    sector: str
    market: str
    is_etf: bool = False
    underlying: str | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None
    multiplier: int = 1
    adjusted: bool = False
    occ_verified: bool = True

    def __post_init__(self) -> None:
        if not all((self.symbol, self.issuer, self.sector, self.market)) or self.multiplier <= 0:
            raise ValueError("instrument identity and positive multiplier are required")
        terms = (self.underlying, self.expiry, self.strike, self.right)
        if self.asset_type is AssetType.OPTION:
            if any(v is None for v in terms) or self.market != "US" or self.multiplier != 100:
                raise ValueError("options must be standard US-listed equity/ETF contracts")
        elif any(v is not None for v in terms) or self.multiplier != 1:
            raise ValueError("shares/ETFs cannot contain option terms")
        elif self.market not in DEVELOPED_MARKETS:
            raise ValueError("shares and mainstream equity ETFs are restricted to developed markets")
        if self.adjusted and self.occ_verified:
            raise ValueError("adjusted contracts must be frozen pending OCC verification")


@dataclass(frozen=True)
class MarketQuote:
    instrument: Instrument
    source: str
    market: str
    currency: str
    fx_to_gbp: Decimal
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int
    ask_size: int
    market_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.market_at, "quote.market_at")
        require_utc(self.available_at, "quote.available_at")
        if not self.source or not self.currency or self.fx_to_gbp <= 0 or self.market != self.instrument.market:
            raise ValueError("quote source and matching market are required")
        if self.available_at < self.market_at:
            raise ValueError("quote cannot be available before its market timestamp")
        if self.bid is None or self.ask is None or self.bid < 0 or self.ask <= 0:
            raise ValueError("one-sided or non-positive quote is not actionable")
        if self.bid > self.ask or self.bid_size <= 0 or self.ask_size <= 0:
            raise ValueError("crossed or zero-size quote is not actionable")


@dataclass(frozen=True)
class Order:
    order_id: str
    instrument: Instrument
    side: Side
    quantity: int
    intent: str
    exit_entire_holding: bool = False
    sale_floor: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_id or self.quantity <= 0:
            raise ValueError("order id and positive quantity are required")
        if self.intent not in {"open", "close", "assign"}:
            raise ValueError("unsupported order intent")


@dataclass(frozen=True)
class Observation:
    name: str
    value: str
    available_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.available_at, "observation.available_at")


@dataclass(frozen=True)
class ResearchCandidate:
    underlying: str
    thesis: str

    def __post_init__(self) -> None:
        if not self.underlying or not self.thesis:
            raise ValueError("research candidate needs an underlying and thesis")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


class SealedRecord:
    seal: str

    def canonical_bytes(self) -> bytes:
        payload = asdict(self)  # type: ignore[arg-type]
        payload.pop("seal")
        return json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()

    def expected_seal(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def sealed(self):
        if self.seal:
            raise ValueError("record is already sealed")
        return replace(self, seal=self.expected_seal())

    def verify(self) -> bool:
        return bool(self.seal) and self.seal == self.expected_seal()


@dataclass(frozen=True)
class ResearchPacket(SealedRecord):
    """The 13:30 Europe/London shortlist; exact contracts are impossible here."""

    packet_id: str
    mandate_version: str
    research_at: datetime
    shortlist: tuple[ResearchCandidate, ...]
    observations: tuple[Observation, ...]
    rationale: str
    seal: str = ""

    def __post_init__(self) -> None:
        require_utc(self.research_at, "research_at")
        if self.research_at.astimezone(LONDON).time().replace(tzinfo=None) != time(13, 30):
            raise ValueError("research packet must be created at 13:30 Europe/London")
        if not self.packet_id or not self.shortlist or not self.rationale:
            raise ValueError("packet identity, shortlist and rationale are required")
        if any(o.available_at > self.research_at for o in self.observations):
            raise ValueError("hindsight detected in research packet")


@dataclass(frozen=True)
class TradingDecision(SealedRecord):
    decision_id: str
    research_packet_id: str
    decision_at: datetime
    selected_order: Order
    observations: tuple[Observation, ...]
    seal: str = ""

    def __post_init__(self) -> None:
        require_utc(self.decision_at, "decision_at")
        if self.decision_at.astimezone(LONDON).time().replace(tzinfo=None) != time(14, 40):
            raise ValueError("trading decision must be created at 14:40 Europe/London")
        if any(o.available_at > self.decision_at for o in self.observations):
            raise ValueError("hindsight detected in trading decision")


@dataclass(frozen=True)
class OrderSubmission:
    decision_id: str
    decision_at: datetime
    order: Order
    submitted_at: datetime

    def __post_init__(self) -> None:
        require_utc(self.submitted_at, "submitted_at")
        require_utc(self.decision_at, "decision_at")
        if self.submitted_at < self.decision_at:
            raise ValueError("order submission cannot predate its decision")
