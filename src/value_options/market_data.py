"""Provider-neutral, sealed and strictly read-only market-data evidence."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import UTC, require_utc


def canonical_json(value: Any) -> bytes:
    """RFC-8259-compatible deterministic JSON (providers must supply JSON values)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class EvidenceKind(str, Enum):
    CLOCK = "market_clock"
    CALENDAR = "trading_calendar"
    UNDERLYING_QUOTE = "underlying_quote"
    OPTION_CHAIN = "option_chain"
    OPTION_QUOTE = "option_quote"
    CORPORATE_ACTION = "corporate_action"
    DIVIDEND = "dividend"
    FX = "gbp_usd_fx"


@dataclass(frozen=True)
class EvidencePacket:
    packet_id: str
    kind: EvidenceKind
    provider: str
    feed: str
    request: Mapping[str, Any]
    requested_at: datetime
    received_at: datetime
    raw: Any
    normalized: Mapping[str, Any]
    raw_sha256: str = ""
    seal: str = ""

    def __post_init__(self) -> None:
        require_utc(self.requested_at, "requested_at")
        require_utc(self.received_at, "received_at")
        if not self.packet_id or not self.provider or not self.feed:
            raise ValueError("packet, provider and quote-feed identity are required")
        if self.received_at < self.requested_at:
            raise ValueError("response predates request")

    def _document(self) -> dict[str, Any]:
        return {"packet_id": self.packet_id, "kind": self.kind.value,
                "provider": self.provider, "feed": self.feed,
                "request": self.request, "requested_at": self.requested_at.isoformat(),
                "received_at": self.received_at.isoformat(), "raw": self.raw,
                "normalized": self.normalized,
                "raw_sha256": self.raw_sha256}

    def sealed(self) -> "EvidencePacket":
        if self.seal or self.raw_sha256:
            raise ValueError("packet is already sealed")
        raw_hash = hashlib.sha256(canonical_json(self.raw)).hexdigest()
        packet = replace(self, raw_sha256=raw_hash)
        return replace(packet, seal=hashlib.sha256(canonical_json(packet._document())).hexdigest())

    def verify(self) -> bool:
        if not self.raw_sha256 or not self.seal:
            return False
        return (self.raw_sha256 == hashlib.sha256(canonical_json(self.raw)).hexdigest()
                and self.seal == hashlib.sha256(canonical_json(self._document())).hexdigest())

    def as_json(self) -> dict[str, Any]:
        return {**self._document(), "seal": self.seal, "classification": "PAPER ONLY",
                "order_policy": "NO LIVE ORDER"}


@dataclass(frozen=True)
class Assessment:
    accepted: bool
    reasons: tuple[str, ...] = ()


def assess(packet: EvidencePacket, *, as_of: datetime, cutoff: datetime,
           max_age: timedelta = timedelta(minutes=15), expected_symbol: str | None = None) -> Assessment:
    """Fail closed without synthesising any absent field."""
    require_utc(as_of, "as_of"); require_utc(cutoff, "cutoff")
    n, reasons = packet.normalized, []
    if not packet.verify(): reasons.append("unverifiable")
    observed = n.get("timestamp")
    try: timestamp = datetime.fromisoformat(observed) if isinstance(observed, str) else None
    except ValueError: timestamp = None
    if timestamp is None or timestamp.tzinfo is None: reasons.append("missing timestamp")
    else:
        timestamp = timestamp.astimezone(UTC)
        if timestamp > as_of: reasons.append("future-dated")
        if as_of - timestamp > max_age: reasons.append("stale")
        if timestamp > cutoff or packet.received_at > cutoff: reasons.append("post-cutoff")
    if expected_symbol and n.get("symbol") != expected_symbol: reasons.append("mismatched")
    if packet.kind in {EvidenceKind.UNDERLYING_QUOTE, EvidenceKind.OPTION_QUOTE, EvidenceKind.FX}:
        bid, ask = n.get("bid"), n.get("ask")
        bs, ass = n.get("bid_size"), n.get("ask_size")
        if bid is None or ask is None: reasons.append("one-sided")
        else:
            try:
                if Decimal(str(bid)) > Decimal(str(ask)): reasons.append("crossed")
            except Exception: reasons.append("unverifiable price")
        if not isinstance(bs, int) or not isinstance(ass, int) or bs <= 0 or ass <= 0:
            reasons.append("zero-size")
    return Assessment(not reasons, tuple(dict.fromkeys(reasons)))


class ReadOnlyMarketData(Protocol):
    def fetch(self, kind: EvidenceKind, request: Mapping[str, Any]) -> Any: ...


@dataclass
class AppendOnlyEvidenceStore:
    """Content-addressed JSONL store: same ID/content is a no-op; mutation is rejected."""
    path: Path
    _seen: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                row = json.loads(line); self._seen[row["packet_id"]] = row["seal"]

    def append(self, packet: EvidencePacket) -> bool:
        if not packet.verify(): raise ValueError("only verified sealed packets may be appended")
        previous = self._seen.get(packet.packet_id)
        if previous:
            if previous != packet.seal: raise ValueError("packet ID collision/tampering")
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(packet.as_json()).decode() + "\n")
        self._seen[packet.packet_id] = packet.seal
        return True
