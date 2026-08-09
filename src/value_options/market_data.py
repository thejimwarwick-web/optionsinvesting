"""Provider-neutral, tamper-evident, strictly read-only market evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Protocol

from .models import UTC, require_utc


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class EvidenceKind(str, Enum):
    CLOCK = "market_clock"; CALENDAR = "trading_calendar"
    UNDERLYING_QUOTE = "underlying_quote"; OPTION_CHAIN = "option_chain"
    OPTION_QUOTE = "option_quote"; CORPORATE_ACTION = "corporate_action"
    DIVIDEND = "dividend"; FX = "gbp_usd_fx"


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
    raw_sha256: str
    seal: str

    def __post_init__(self) -> None:
        require_utc(self.requested_at, "requested_at"); require_utc(self.received_at, "received_at")

    def content(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "provider": self.provider, "feed": self.feed,
                "request": self.request, "requested_at": self.requested_at.isoformat(),
                "received_at": self.received_at.isoformat(), "raw": self.raw,
                "normalized": self.normalized}

    def envelope(self) -> dict[str, Any]:
        return {"packet_id": self.packet_id, **self.content(), "raw_sha256": self.raw_sha256}

    def verify_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.provider or not self.feed: reasons.append("missing source identity")
        if self.received_at < self.requested_at: reasons.append("response predates request")
        expected_raw = hashlib.sha256(canonical_json(self.raw)).hexdigest()
        expected_id = hashlib.sha256(canonical_json(self.content())).hexdigest()
        expected_seal = hashlib.sha256(canonical_json(self.envelope())).hexdigest()
        if self.raw_sha256 != expected_raw: reasons.append("raw hash mismatch")
        if self.packet_id != expected_id: reasons.append("packet ID is not content-addressed")
        if self.seal != expected_seal: reasons.append("seal mismatch")
        return tuple(reasons)

    def verify(self) -> bool: return not self.verify_reasons()

    def as_json(self) -> dict[str, Any]:
        return {**self.envelope(), "seal": self.seal, "classification": "PAPER ONLY",
                "order_policy": "NO LIVE ORDER"}


def ingest_response(*, kind: EvidenceKind, provider: str, feed: str,
                    request: Mapping[str, Any], requested_at: datetime,
                    received_at: datetime, raw: Any,
                    normalized: Mapping[str, Any]) -> EvidencePacket:
    """Create and seal provider evidence exactly once; imported packets never use this path."""
    content = {"kind": kind.value, "provider": provider, "feed": feed, "request": request,
               "requested_at": requested_at.isoformat(), "received_at": received_at.isoformat(),
               "raw": raw, "normalized": normalized}
    packet_id = hashlib.sha256(canonical_json(content)).hexdigest()
    raw_hash = hashlib.sha256(canonical_json(raw)).hexdigest()
    envelope = {"packet_id": packet_id, **content, "raw_sha256": raw_hash}
    return EvidencePacket(packet_id, kind, provider, feed, request, requested_at, received_at,
                          raw, normalized, raw_hash,
                          hashlib.sha256(canonical_json(envelope)).hexdigest())


def load_packet(value: Mapping[str, Any]) -> EvidencePacket:
    """Load supplied cryptographic fields verbatim. It deliberately cannot reseal."""
    return EvidencePacket(str(value.get("packet_id", "")), EvidenceKind(value["kind"]),
                          str(value.get("provider", "")), str(value.get("feed", "")),
                          value.get("request", {}), datetime.fromisoformat(value["requested_at"]),
                          datetime.fromisoformat(value["received_at"]), value.get("raw"),
                          value.get("normalized", {}), str(value.get("raw_sha256", "")),
                          str(value.get("seal", "")))


@dataclass(frozen=True)
class Assessment:
    verified: bool
    actionable: bool
    quarantined: bool
    excluded: bool
    reasons: tuple[str, ...]


def assess(packet: EvidencePacket, *, as_of: datetime, cutoff: datetime,
           max_age: timedelta | None, expected_symbol: str | None = None,
           excluded: bool = False) -> Assessment:
    require_utc(as_of, "as_of"); require_utc(cutoff, "cutoff")
    reasons = list(packet.verify_reasons()); n = packet.normalized
    if packet.received_at > as_of: reasons.append("received after as-of")
    if packet.received_at > cutoff: reasons.append("received post-cutoff")
    observed = n.get("timestamp")
    try: timestamp = datetime.fromisoformat(observed).astimezone(UTC) if isinstance(observed, str) else None
    except ValueError: timestamp = None
    # Calendars describe session coverage, not a point-in-time market observation.
    # Their provenance is bounded by requested_at/received_at instead.
    timestamp_optional=(packet.kind is EvidenceKind.CALENDAR or
        packet.kind in {EvidenceKind.CORPORATE_ACTION,EvidenceKind.DIVIDEND} and n.get("negative_evidence") is True)
    if timestamp is None and not timestamp_optional: reasons.append("missing timestamp")
    elif timestamp is not None:
        if timestamp > as_of: reasons.append("future-dated")
        if timestamp > cutoff: reasons.append("observed post-cutoff")
        if max_age is not None and as_of - timestamp > max_age: reasons.append("stale")
    if expected_symbol and n.get("symbol") != expected_symbol: reasons.append("mismatched symbol")
    if packet.kind in {EvidenceKind.UNDERLYING_QUOTE, EvidenceKind.OPTION_QUOTE, EvidenceKind.FX}:
        bid, ask, bs, az = n.get("bid"), n.get("ask"), n.get("bid_size"), n.get("ask_size")
        if bid is None or ask is None: reasons.append("one-sided")
        else:
            try:
                bid_value, ask_value = Decimal(str(bid)), Decimal(str(ask))
                if not bid_value.is_finite() or not ask_value.is_finite(): reasons.append("non-finite price")
                elif bid_value <= 0 or ask_value <= 0: reasons.append("non-positive price")
                elif bid_value > ask_value: reasons.append("crossed")
            except (InvalidOperation, ValueError): reasons.append("unverifiable price")
        # Spot FX venues frequently publish no exchange size.  Absence is valid;
        # an explicitly supplied size, however, must be positive.
        if packet.kind is EvidenceKind.FX:
            if (bs is not None and (not isinstance(bs, int) or bs <= 0)) or \
               (az is not None and (not isinstance(az, int) or az <= 0)):
                reasons.append("invalid FX size")
            try:
                mid = Decimal(str(n.get("mid")))
                if not mid.is_finite() or mid <= 0: reasons.append("invalid FX mid")
            except (InvalidOperation, ValueError): reasons.append("invalid FX mid")
        elif not isinstance(bs, int) or not isinstance(az, int) or bs <= 0 or az <= 0:
            reasons.append("zero-size")
    if packet.kind is EvidenceKind.CALENDAR:
        session = n.get("session_date")
        if session != as_of.date().isoformat(): reasons.append("calendar does not cover relevant session")
    if packet.kind in {EvidenceKind.CORPORATE_ACTION, EvidenceKind.DIVIDEND}:
        if not n.get("effective_date"): reasons.append("missing effective date")
        if not n.get("retrieved_at"): reasons.append("missing retrieval timestamp")
        else:
            try:
                retrieved = datetime.fromisoformat(str(n["retrieved_at"])).astimezone(UTC)
                if retrieved > as_of: reasons.append("retrieved after as-of")
            except (ValueError, TypeError): reasons.append("malformed retrieval timestamp")
    if packet.kind is EvidenceKind.OPTION_QUOTE:
        if packet.feed.upper() != "OPRA": reasons.append("option quote is not OPRA")
        for field in ("symbol", "underlying", "expiration", "strike", "right",
                      "multiplier", "currency", "market"):
            if n.get(field) in (None, ""): reasons.append(f"missing option {field}")
    reasons = list(dict.fromkeys(reasons)); verified = packet.verify()
    return Assessment(verified, verified and not reasons and not excluded,
                      bool(reasons) and not excluded, excluded, tuple(reasons))


class ReadOnlyMarketData(Protocol):
    def fetch(self, kind: EvidenceKind, request: Mapping[str, Any]) -> Any: ...


class AppendOnlyEvidenceStore:
    """Atomic, locked JSONL replacement; duplicate IDs are compared byte-for-byte."""
    def __init__(self, path: Path): self.path = path

    def append(self, packet: EvidencePacket) -> bool:
        if not packet.verify(): raise ValueError("only verified content-addressed packets may be appended")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = [json.loads(x) for x in self.path.read_text().splitlines()] if self.path.exists() else []
            document = packet.as_json()
            for row in rows:
                if row["packet_id"] == packet.packet_id:
                    if canonical_json(row) != canonical_json(document):
                        raise ValueError("duplicate packet ID has different content")
                    return False
            rows.append(document)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    for row in rows: stream.write(canonical_json(row).decode() + "\n")
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name): os.unlink(name)
            return True
