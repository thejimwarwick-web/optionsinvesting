"""Operational paper-only research, decision, fill and excluded-replay flow."""

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .market_data import EvidencePacket, assess
from .models import UTC, require_utc

LONDON = ZoneInfo("Europe/London")


@dataclass
class PaperRun:
    launch_status: str = "PAPER ONLY"
    cash: Decimal = Decimal("100000")
    nav: Decimal = Decimal("100000")
    orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    research_at: datetime | None = None
    decision_at: datetime | None = None
    submitted_at: datetime | None = None
    quote_at: datetime | None = None
    fill_at: datetime | None = None

    def research(self, at: datetime, underlyings: list[str]) -> dict[str, Any]:
        require_utc(at, "research_at")
        if at.astimezone(LONDON).time().replace(tzinfo=None) != time(13, 30):
            raise ValueError("research must occur at 13:30 Europe/London")
        self.research_at = at
        return {"underlyings": underlyings, "exact_contract": None, "sealed": True}

    def submit(self, at: datetime, contract: str, side: str, quantity: int, *,
               mandate_approved: bool, risk_approved: bool) -> dict[str, Any]:
        require_utc(at, "decision_at")
        if at.astimezone(LONDON).time().replace(tzinfo=None) != time(14, 40):
            raise ValueError("decision must occur at 14:40 Europe/London")
        if not self.research_at: raise ValueError("sealed research is required")
        if not mandate_approved or not risk_approved:
            raise ValueError("mandate and risk checks must both approve")
        self.decision_at = at; self.submitted_at = at
        order = {"contract": contract, "side": side, "quantity": quantity,
                 "decision_at": at.isoformat(), "submitted_at": at.isoformat(),
                 "mandate_approved": True, "risk_approved": True,
                 "status": "SIMULATED", "notice": "NO LIVE ORDER"}
        self.orders.append(order); return order

    def fill(self, packet: EvidencePacket) -> dict[str, Any]:
        if not self.submitted_at: raise ValueError("submission is required")
        observed = datetime.fromisoformat(str(packet.normalized.get("timestamp"))).astimezone(UTC)
        check = assess(packet, as_of=packet.received_at, cutoff=packet.received_at,
                       expected_symbol=self.orders[-1]["contract"])
        if not check.accepted: raise ValueError("quote quarantined: " + ", ".join(check.reasons))
        if observed <= self.submitted_at: raise ValueError("fill quote must be observed after submission")
        side = self.orders[-1]["side"]
        price = packet.normalized["ask" if side == "buy" else "bid"]
        self.quote_at = observed; self.fill_at = packet.received_at
        return {"price": price, "side": side, "quote_at": observed.isoformat(),
                "fill_at": self.fill_at.isoformat(), "notice": "PAPER ONLY — NO LIVE ORDER"}

    def excluded_replay(self, packets: list[EvidencePacket]) -> dict[str, Any]:
        before = (self.launch_status, self.cash, self.nav, list(self.orders), list(self.positions))
        result = {"excluded": True, "packet_seals": [p.seal for p in packets],
                  "notice": "PAPER ONLY — NO LIVE ORDER"}
        assert before == (self.launch_status, self.cash, self.nav, self.orders, self.positions)
        return result
