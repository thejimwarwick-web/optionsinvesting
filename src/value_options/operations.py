"""Integrated, paper-only operational orchestration."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .mandate import FundMandate
from .market_data import Assessment, EvidenceKind, EvidencePacket, assess, canonical_json
from .models import (AssetType, Instrument, Observation, OptionRight, Order, OrderSubmission,
                     ResearchCandidate, ResearchPacket, Side, TradingDecision, require_utc)
from .risk import PortfolioRisk, RiskEngine, RiskViolation

LONDON = ZoneInfo("Europe/London")
REQUIRED = frozenset(EvidenceKind)


@dataclass(frozen=True)
class SealedRuleResults:
    mandate_version: str
    evaluated_at: datetime
    results: tuple[tuple[str, bool, str], ...]
    seal: str

    @classmethod
    def create(cls, mandate: FundMandate, at: datetime,
               results: tuple[tuple[str, bool, str], ...]) -> "SealedRuleResults":
        body = {"mandate_version": mandate.version, "evaluated_at": at.isoformat(), "results": results}
        return cls(mandate.version, at, results, hashlib.sha256(canonical_json(body)).hexdigest())

    def verify(self) -> bool:
        body = {"mandate_version": self.mandate_version,
                "evaluated_at": self.evaluated_at.isoformat(), "results": self.results}
        return self.seal == hashlib.sha256(canonical_json(body)).hexdigest()


@dataclass(frozen=True)
class OperationalReport:
    classification: str
    order_policy: str
    verified: bool
    actionable: bool
    quarantined: bool
    excluded: bool
    reasons: tuple[str, ...]
    assessments: Mapping[str, Assessment]

    def jsonable(self) -> dict[str, Any]:
        return {"classification": self.classification, "order_policy": self.order_policy,
                "verified": self.verified, "actionable": self.actionable,
                "quarantined": self.quarantined, "excluded": self.excluded,
                "reasons": self.reasons,
                "evidence": {k: vars(v) for k, v in self.assessments.items()}}


class PaperRun:
    def __init__(self, mandate: FundMandate, portfolio: PortfolioRisk,
                 *, tolerance: timedelta = timedelta(minutes=5)):
        self.mandate, self.portfolio, self.tolerance = mandate, portfolio, tolerance
        self.launch_status = "PAPER ONLY"; self.cash = portfolio.cash_gbp; self.nav = portfolio.nav_gbp
        self.orders: list[OrderSubmission] = []; self.positions = dict(portfolio.positions)
        self.research_packet: ResearchPacket | None = None
        self.decision: TradingDecision | None = None
        self.rule_results: SealedRuleResults | None = None

    def _in_window(self, at: datetime, start: time, label: str) -> None:
        require_utc(at, label); local = at.astimezone(LONDON)
        scheduled = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if not scheduled <= local <= scheduled + self.tolerance:
            raise ValueError(f"{label} outside operational tolerance")

    def create_research(self, at: datetime, candidates: tuple[ResearchCandidate, ...]) -> ResearchPacket:
        self._in_window(at, time(13, 30), "research")
        packet = ResearchPacket(hashlib.sha256(canonical_json([vars(x) for x in candidates])).hexdigest(),
                                self.mandate.version, at, candidates, (), "underlying-only shortlist").sealed()
        self.research_packet = packet; return packet

    def decide(self, at: datetime, order: Order, evidence: Mapping[EvidenceKind, EvidencePacket]) -> TradingDecision:
        self._in_window(at, time(14, 40), "decision")
        if not self.research_packet or not self.research_packet.verify(): raise ValueError("sealed research required")
        report = self.assess_bundle(evidence, as_of=at, cutoff=at)
        results: list[tuple[str, bool, str]] = [("complete_actionable_evidence", report.actionable,
                                                "; ".join(report.reasons) or "passed")]
        risk_ok, risk_reason = True, "passed"
        try: RiskEngine(self.mandate).check(order, self.portfolio, at.date(), self._fx(evidence))
        except RiskViolation as error: risk_ok, risk_reason = False, str(error)
        results.append(("mandate_and_risk_engine", risk_ok, risk_reason))
        self.rule_results = SealedRuleResults.create(self.mandate, at, tuple(results))
        if not all(result[1] for result in results): raise ValueError("decision checks failed: " + "; ".join(report.reasons + (risk_reason,)))
        chain_reasons = self._contract_reasons(order.instrument, evidence)
        if chain_reasons: raise ValueError("contract evidence mismatch: " + ", ".join(chain_reasons))
        observations = tuple(Observation(kind.value, packet.seal, packet.received_at) for kind, packet in evidence.items())
        decision = TradingDecision(hashlib.sha256(canonical_json({"order": order.order_id, "at": at.isoformat()})).hexdigest(),
                                   self.research_packet.packet_id, at, order, observations).sealed()
        self.decision = decision; return decision

    def submit(self, at: datetime) -> OrderSubmission:
        require_utc(at, "submitted_at")
        if (not self.decision or not self.decision.verify() or not self.rule_results
                or not self.rule_results.verify()
                or not all(result[1] for result in self.rule_results.results)):
            raise ValueError("sealed decision and executed rule results required")
        submission = OrderSubmission(self.decision.decision_id, self.decision.decision_at,
                                     self.decision.selected_order, at)
        self.orders.append(submission); return submission

    def simulate_fill(self, packet: EvidencePacket, *, as_of: datetime) -> dict[str, Any]:
        if not self.orders: raise ValueError("submission required")
        submission = self.orders[-1]; instrument = submission.order.instrument
        reasons = self._quote_contract_reasons(packet, instrument)
        check = assess(packet, as_of=as_of, cutoff=as_of,
                       max_age=timedelta(seconds=self.mandate.max_quote_age_seconds),
                       expected_symbol=instrument.symbol)
        observed = self._timestamp(packet)
        if observed <= submission.submitted_at: reasons.append("quote observed before or at submission")
        if packet.received_at <= submission.submitted_at: reasons.append("quote available before or at submission")
        if as_of <= packet.received_at: reasons.append("fill must follow quote availability")
        reasons.extend(check.reasons)
        if reasons: raise ValueError("quote quarantined: " + ", ".join(dict.fromkeys(reasons)))
        return {"price": packet.normalized["ask" if submission.order.side is Side.BUY else "bid"],
                "decision_at": submission.decision_at.isoformat(),
                "submitted_at": submission.submitted_at.isoformat(), "quote_at": observed.isoformat(),
                "quote_available_at": packet.received_at.isoformat(), "filled_at": as_of.isoformat(),
                "notice": "PAPER ONLY — NO LIVE ORDER"}

    def assess_bundle(self, evidence: Mapping[EvidenceKind, EvidencePacket], *, as_of: datetime,
                      cutoff: datetime, excluded: bool = False) -> OperationalReport:
        missing = sorted(x.value for x in REQUIRED - evidence.keys())
        assessments = {kind.value: assess(packet, as_of=as_of, cutoff=cutoff,
                           max_age=timedelta(seconds=self.mandate.max_quote_age_seconds), excluded=excluded)
                       for kind, packet in evidence.items()}
        reasons = [f"missing evidence family: {x}" for x in missing]
        for name, result in assessments.items(): reasons.extend(f"{name}: {x}" for x in result.reasons)
        verified = not missing and all(x.verified for x in assessments.values())
        actionable = not excluded and not reasons and all(x.actionable for x in assessments.values())
        return OperationalReport("PAPER ONLY", "NO LIVE ORDER", verified, actionable,
                                 not excluded and not actionable, excluded, tuple(reasons), assessments)

    def excluded_replay(self, evidence: Mapping[EvidenceKind, EvidencePacket], *, as_of: datetime) -> OperationalReport:
        before = self._state_fingerprint()
        report = self.assess_bundle(evidence, as_of=as_of, cutoff=as_of, excluded=True)
        if before != self._state_fingerprint():
            raise RuntimeError("excluded replay mutated operational state")
        return report

    def _state_fingerprint(self) -> bytes:
        return canonical_json({"launch": self.launch_status, "cash": str(self.cash), "nav": str(self.nav),
                               "orders": [x.order.order_id for x in self.orders],
                               "positions": sorted((x.symbol, y.quantity) for x, y in self.positions.items())})

    @staticmethod
    def _timestamp(packet: EvidencePacket) -> datetime:
        return datetime.fromisoformat(str(packet.normalized.get("timestamp"))).astimezone(timezone.utc)

    @staticmethod
    def _fx(evidence: Mapping[EvidenceKind, EvidencePacket]) -> Decimal:
        return Decimal(str(evidence[EvidenceKind.FX].normalized["mid"]))

    @staticmethod
    def _quote_contract_reasons(packet: EvidencePacket, i: Instrument) -> list[str]:
        n = packet.normalized; reasons = []
        if packet.kind is not EvidenceKind.OPTION_QUOTE: reasons.append("not option-quote evidence")
        if packet.feed.upper() != "OPRA": reasons.append("option quote is not OPRA")
        expected = {"symbol": i.symbol, "underlying": i.underlying, "expiration": i.expiry.isoformat(),
                    "strike": str(i.strike), "right": i.right.value, "multiplier": i.multiplier,
                    "currency": "USD", "market": "US"}
        for key, value in expected.items():
            if n.get(key) != value: reasons.append(f"option {key} mismatch")
        return reasons

    def _contract_reasons(self, i: Instrument, evidence: Mapping[EvidenceKind, EvidencePacket]) -> list[str]:
        reasons = self._quote_contract_reasons(evidence[EvidenceKind.OPTION_QUOTE], i)
        contracts = evidence[EvidenceKind.OPTION_CHAIN].normalized.get("contracts", [])
        expected = {"symbol": i.symbol, "underlying": i.underlying, "expiration": i.expiry.isoformat(),
                    "strike": str(i.strike), "right": i.right.value, "multiplier": i.multiplier}
        if not any(all(c.get(k) == v for k, v in expected.items()) for c in contracts):
            reasons.append("exact contract absent from option chain")
        return reasons
