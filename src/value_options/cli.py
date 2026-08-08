"""Offline inspection and operational reporting; no write-side adapters exist."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from .mandate import DEFAULT_MANDATE
from .market_data import EvidenceKind, load_packet
from .operations import PaperRun
from .risk import PortfolioRisk
from .models import (AssetType, Instrument, OptionRight, Order, ResearchCandidate, Side)


def load(path: Path):
    return load_packet(json.loads(path.read_text()))


def load_bundle(path: Path):
    value = json.loads(path.read_text()); rows = value["packets"]
    return ({packet.kind: packet for packet in (load_packet(row) for row in rows)}, value.get("operation"))


def _run() -> PaperRun:
    return PaperRun(DEFAULT_MANDATE, PortfolioRisk(Decimal("100000"), Decimal("100000"),
                    Decimal("100000"), {}, {}))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="value-options"); sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect"); inspect.add_argument("packet", type=Path)
    inspect.add_argument("--as-of", required=True); inspect.add_argument("--output", type=Path, required=True)
    for name in ("dry-run", "replay"):
        command = sub.add_parser(name); command.add_argument("bundle", type=Path)
        command.add_argument("--as-of", required=True); command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv); as_of = datetime.fromisoformat(args.as_of).astimezone(timezone.utc); run = _run()
    if args.command == "inspect":
        packet = load(args.packet)
        from .market_data import assess
        result = assess(packet, as_of=as_of, cutoff=as_of,
                        max_age=__import__('datetime').timedelta(seconds=DEFAULT_MANDATE.max_quote_age_seconds))
        artifact = {"mode": "inspection", **asdict(result)}
    else:
        evidence, operation = load_bundle(args.bundle)
        if args.command == "replay":
            report = run.excluded_replay(evidence, as_of=as_of); events = {}
        elif not operation:
            report = run.assess_bundle(evidence, as_of=as_of, cutoff=as_of)
            report = report.__class__(report.classification, report.order_policy, report.verified, False,
                                      True, False, report.reasons + ("missing operational specification",), report.assessments)
            events = {}
        else:
            research_at = datetime.fromisoformat(operation["research_at"]).astimezone(timezone.utc)
            decision_at = datetime.fromisoformat(operation["decision_at"]).astimezone(timezone.utc)
            submitted_at = datetime.fromisoformat(operation["submitted_at"]).astimezone(timezone.utc)
            spec = operation["instrument"]
            instrument = Instrument(spec["symbol"], AssetType.OPTION, spec["issuer"], spec["sector"],
                                    "US", underlying=spec["underlying"],
                                    expiry=datetime.fromisoformat(spec["expiration"]).date(),
                                    strike=Decimal(spec["strike"]), right=OptionRight(spec["right"]), multiplier=100)
            order = Order(operation["order_id"], instrument, Side(operation["side"]),
                          int(operation["quantity"]), operation["intent"])
            research = run.create_research(research_at, (ResearchCandidate(instrument.underlying, operation["thesis"]),))
            decision = run.decide(decision_at, order, evidence); submission = run.submit(submitted_at)
            report = run.assess_bundle(evidence, as_of=decision_at, cutoff=decision_at)
            events = {"research_at": research.research_at.isoformat(), "decision_at": decision.decision_at.isoformat(),
                      "submitted_at": submission.submitted_at.isoformat(), "rule_results_seal": run.rule_results.seal}
        artifact = {"mode": args.command, **report.jsonable(), "events": events}
    artifact.update({"classification": "PAPER ONLY", "order_policy": "NO LIVE ORDER"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    print(f"PAPER ONLY | NO LIVE ORDER | {args.command} | actionable={artifact['actionable']} | reasons={len(artifact['reasons'])}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
