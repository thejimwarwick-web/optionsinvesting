"""Sequential, sealed and strictly paper-only operational orchestration."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from .mandate import FundMandate
from .market_data import Assessment, EvidenceKind, EvidencePacket, assess, canonical_json
from .models import (Observation, Order, OrderSubmission, ResearchCandidate, ResearchPacket,
                     Side, TradingDecision, require_utc)
from .risk import PortfolioRisk, RiskEngine, RiskViolation

LONDON = ZoneInfo("Europe/London")
REQUIRED = frozenset(EvidenceKind)
QUOTE_KINDS = frozenset({EvidenceKind.UNDERLYING_QUOTE, EvidenceKind.OPTION_QUOTE})


def seal_artifact(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create local tamper evidence, not proof of time, author, or launch approval."""
    content = {"artifact_kind": kind, "payload": payload, "classification": "PAPER ONLY",
               "order_policy": "NO LIVE ORDER", "externally_attested": False,
               "launch_eligible": False, "verified": True, "actionable": True,
               "quarantined": False}
    artifact_id = hashlib.sha256(canonical_json(content)).hexdigest()
    envelope = {"artifact_id": artifact_id, **content}
    return {**envelope, "seal": hashlib.sha256(canonical_json(envelope)).hexdigest()}


def verify_artifact(value: Mapping[str, Any], expected_kind: str) -> tuple[bool, tuple[str, ...]]:
    reasons = []
    content = {k: value.get(k) for k in ("artifact_kind", "payload", "classification", "order_policy",
                                         "externally_attested", "launch_eligible", "verified",
                                         "actionable", "quarantined")}
    expected_id = hashlib.sha256(canonical_json(content)).hexdigest()
    envelope = {"artifact_id": value.get("artifact_id"), **content}
    if value.get("artifact_kind") != expected_kind: reasons.append("wrong artifact kind")
    if value.get("externally_attested") is not False: reasons.append("local artifact must be unattested")
    if value.get("launch_eligible") is not False: reasons.append("local artifact must be launch-ineligible")
    if value.get("artifact_id") != expected_id: reasons.append("artifact ID is not content-addressed")
    if value.get("seal") != hashlib.sha256(canonical_json(envelope)).hexdigest(): reasons.append("artifact seal mismatch")
    return not reasons, tuple(reasons)


@dataclass(frozen=True)
class SealedRuleResults:
    mandate_version: str; evaluated_at: datetime
    results: tuple[tuple[str, bool, str], ...]; seal: str

    @classmethod
    def create(cls, mandate: FundMandate, at: datetime, results: tuple[tuple[str, bool, str], ...]):
        body = {"mandate_version": mandate.version, "evaluated_at": at.isoformat(), "results": results}
        return cls(mandate.version, at, results, hashlib.sha256(canonical_json(body)).hexdigest())

    def verify(self) -> bool:
        body = {"mandate_version": self.mandate_version, "evaluated_at": self.evaluated_at.isoformat(), "results": self.results}
        return self.seal == hashlib.sha256(canonical_json(body)).hexdigest()


@dataclass(frozen=True)
class OperationalReport:
    classification: str; order_policy: str; verified: bool; actionable: bool
    quarantined: bool; excluded: bool; reasons: tuple[str, ...]
    assessments: tuple[tuple[str, Assessment], ...]
    selections: tuple[str, ...] = ()

    def jsonable(self) -> dict[str, Any]:
        return {"classification": self.classification, "order_policy": self.order_policy,
                "verified": self.verified, "actionable": self.actionable,
                "quarantined": self.quarantined, "excluded": self.excluded,
                "reasons": self.reasons, "selections": self.selections,
                "evidence": [{"packet_id": packet_id, **asdict(result)}
                             for packet_id, result in self.assessments]}


class PaperRun:
    def __init__(self, mandate: FundMandate, portfolio: PortfolioRisk, *, tolerance=timedelta(minutes=5)):
        self.mandate, self.portfolio, self.tolerance = mandate, portfolio, tolerance
        self.launch_status = "PAPER ONLY"; self.cash = portfolio.cash_gbp; self.nav = portfolio.nav_gbp
        self.orders = []; self.positions = dict(portfolio.positions)
        self.research_packet = None; self.decision = None; self.rule_results = None

    def _in_window(self, at, start, label):
        require_utc(at, label); local = at.astimezone(LONDON)
        scheduled = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if not scheduled <= local <= scheduled + self.tolerance: raise ValueError(f"{label} outside operational tolerance")

    def create_research(self, at: datetime, candidates: tuple[ResearchCandidate, ...],
                        evidence_refs: tuple[Observation, ...] = ()) -> ResearchPacket:
        self._in_window(at, time(13, 30), "research")
        identity = {"research_at": at.isoformat(), "mandate_version": self.mandate.version,
                    "candidates": [asdict(x) for x in candidates],
                    "theses": [x.thesis for x in candidates],
                    "evidence_references": [{"name": x.name, "value": x.value,
                                             "available_at": x.available_at.isoformat()} for x in evidence_refs],
                    "rationale": "underlying-only shortlist"}
        packet = ResearchPacket(hashlib.sha256(canonical_json(identity)).hexdigest(), self.mandate.version,
                                at, candidates, evidence_refs, identity["rationale"]).sealed()
        self.research_packet = packet; return packet

    @staticmethod
    def _rows(evidence: Iterable[EvidencePacket] | Mapping[EvidenceKind, EvidencePacket]):
        return list(evidence.values()) if isinstance(evidence, Mapping) else list(evidence)

    def assess_bundle(self, evidence, *, as_of, cutoff, excluded=False) -> OperationalReport:
        rows = self._rows(evidence); by_kind = {kind: [p for p in rows if p.kind is kind] for kind in EvidenceKind}
        reasons = [f"missing evidence family: {k.value}" for k in EvidenceKind if not by_kind[k]]
        selections = []; selected = {}
        # Quotes are observations: deterministically choose the latest packet available by as-of.
        # All other duplicate families are ambiguous and fail closed.
        for kind, packets in by_kind.items():
            if not packets: continue
            if len(packets) > 1 and kind not in QUOTE_KINDS:
                reasons.append(f"ambiguous duplicate evidence family: {kind.value}")
            chosen = max(packets, key=lambda p: (p.received_at, p.packet_id))
            selected[kind] = chosen
            selections.append(f"{kind.value}: selected {chosen.packet_id}; latest received_at then packet_id; retained {len(packets)} packet(s)")
        assessed = []
        for packet in rows:
            max_age = timedelta(seconds=self.mandate.max_quote_age_seconds) if packet.kind in QUOTE_KINDS else None
            result = assess(packet, as_of=as_of, cutoff=cutoff, max_age=max_age, excluded=excluded)
            assessed.append((packet.packet_id, result))
            reasons.extend(f"{packet.kind.value}[{packet.packet_id}]: {r}" for r in result.reasons)
        reasons = list(dict.fromkeys(reasons)); verified = bool(rows) and all(a.verified for _, a in assessed)
        actionable = not excluded and not reasons and set(selected) == set(REQUIRED)
        return OperationalReport("PAPER ONLY", "NO LIVE ORDER", verified, actionable,
                                 not excluded and not actionable, excluded, tuple(reasons), tuple(assessed), tuple(selections))

    def decide(self, at: datetime, order: Order, evidence) -> TradingDecision:
        self._in_window(at, time(14, 40), "decision")
        if not self.research_packet or not self.research_packet.verify(): raise ValueError("sealed pre-existing research required")
        if order.instrument.underlying not in {c.underlying for c in self.research_packet.shortlist}:
            raise ValueError("contract underlying absent from sealed research shortlist")
        report = self.assess_bundle(evidence, as_of=at, cutoff=at)
        if not report.actionable:
            self.rule_results = SealedRuleResults.create(self.mandate, at, (("complete_actionable_evidence", False, "; ".join(report.reasons)),))
            raise ValueError("evidence quarantined: " + "; ".join(report.reasons))
        rows = self._rows(evidence); selected = {p.kind: p for p in sorted(rows, key=lambda p:(p.received_at,p.packet_id))}
        contract_reasons = self._contract_reasons(order.instrument, selected)
        if contract_reasons: raise ValueError("contract evidence mismatch: " + ", ".join(contract_reasons))
        try:
            fx = Decimal(str(selected[EvidenceKind.FX].normalized["mid"])); RiskEngine(self.mandate).check(order, self.portfolio, at.date(), fx)
            risk_ok, risk_reason = True, "passed"
        except (RiskViolation, KeyError, InvalidOperation, ValueError) as error: risk_ok, risk_reason = False, str(error)
        self.rule_results = SealedRuleResults.create(self.mandate, at, (("complete_actionable_evidence", True, "passed"), ("mandate_and_risk_engine", risk_ok, risk_reason)))
        if not risk_ok: raise ValueError("decision checks failed: " + risk_reason)
        observations = tuple(Observation(p.kind.value, p.seal, p.received_at) for p in rows)
        decision = TradingDecision(hashlib.sha256(canonical_json({"order": order.order_id, "at": at.isoformat(), "research": self.research_packet.packet_id})).hexdigest(), self.research_packet.packet_id, at, order, observations).sealed()
        self.decision = decision; return decision

    def submit(self, at):
        require_utc(at, "submitted_at")
        if not self.decision or not self.decision.verify() or not self.rule_results or not self.rule_results.verify() or not all(x[1] for x in self.rule_results.results): raise ValueError("sealed decision and executed rule results required")
        submission = OrderSubmission(self.decision.decision_id, self.decision.decision_at, self.decision.selected_order, at); self.orders.append(submission); return submission

    def simulate_fill(self, packet, *, as_of):
        if not self.orders: raise ValueError("submission required")
        submission = self.orders[-1]; reasons = self._quote_contract_reasons(packet, submission.order.instrument)
        check = assess(packet, as_of=as_of, cutoff=as_of, max_age=timedelta(seconds=self.mandate.max_quote_age_seconds), expected_symbol=submission.order.instrument.symbol)
        try: observed = datetime.fromisoformat(str(packet.normalized.get("timestamp"))).astimezone(timezone.utc)
        except (ValueError, TypeError): observed = None; reasons.append("malformed quote timestamp")
        if observed and observed <= submission.submitted_at: reasons.append("quote observed before or at submission")
        if packet.received_at <= submission.submitted_at: reasons.append("quote available before or at submission")
        if as_of <= packet.received_at: reasons.append("fill must follow quote availability")
        reasons.extend(check.reasons)
        if reasons: raise ValueError("quote quarantined: " + ", ".join(dict.fromkeys(reasons)))
        return {"price": packet.normalized["ask" if submission.order.side is Side.BUY else "bid"], "decision_at": submission.decision_at.isoformat(), "submitted_at": submission.submitted_at.isoformat(), "quote_at": observed.isoformat(), "quote_available_at": packet.received_at.isoformat(), "filled_at": as_of.isoformat(), "notice": "PAPER ONLY — NO LIVE ORDER"}

    def excluded_replay(self, evidence, *, as_of):
        before = self._state_fingerprint(); report = self.assess_bundle(evidence, as_of=as_of, cutoff=as_of, excluded=True)
        if before != self._state_fingerprint(): raise RuntimeError("excluded replay mutated operational state")
        return report

    def _state_fingerprint(self):
        return canonical_json({"launch":self.launch_status,"cash":str(self.cash),"nav":str(self.nav),"orders":[x.order.order_id for x in self.orders],"positions":sorted((x.symbol,y.quantity) for x,y in self.positions.items())})

    @staticmethod
    def _quote_contract_reasons(packet, i):
        n=packet.normalized; reasons=[]
        if packet.kind is not EvidenceKind.OPTION_QUOTE: reasons.append("not option-quote evidence")
        if packet.feed.upper() != "OPRA": reasons.append("option quote is not OPRA")
        expected={"symbol":i.symbol,"underlying":i.underlying,"expiration":i.expiry.isoformat(),"strike":str(i.strike),"right":i.right.value,"multiplier":i.multiplier,"currency":"USD","market":"US"}
        for k,v in expected.items():
            if n.get(k)!=v: reasons.append(f"option {k} mismatch")
        return reasons

    def _contract_reasons(self, i, evidence):
        q=evidence.get(EvidenceKind.OPTION_QUOTE); chain=evidence.get(EvidenceKind.OPTION_CHAIN)
        if not q or not chain: return ["missing exact option evidence"]
        reasons=self._quote_contract_reasons(q,i); expected={"symbol":i.symbol,"underlying":i.underlying,"expiration":i.expiry.isoformat(),"strike":str(i.strike),"right":i.right.value,"multiplier":i.multiplier}
        contracts=chain.normalized.get("contracts",[]) if isinstance(chain.normalized, Mapping) else []
        if not isinstance(contracts,list) or not any(isinstance(c,Mapping) and all(c.get(k)==v for k,v in expected.items()) for c in contracts): reasons.append("exact contract absent from option chain")
        return reasons
