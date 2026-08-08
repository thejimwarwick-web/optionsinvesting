import inspect, json, os, subprocess, sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from value_options.accounting import Position
from value_options.cli import main
from value_options.mandate import DEFAULT_MANDATE
from value_options.market_data import (AppendOnlyEvidenceStore, EvidenceKind, assess,
                                       ingest_response, load_packet)
from value_options.models import AssetType, Instrument, OptionRight, Order, ResearchCandidate, Side
from value_options.operations import PaperRun
from value_options.risk import PortfolioRisk

UTC = timezone.utc
DECISION = datetime(2026, 8, 7, 13, 42, tzinfo=UTC)
CONTRACT = "AAPL260828P00020000"


def normalized(kind, timestamp=None):
    base = {"timestamp": (timestamp or DECISION - timedelta(seconds=10)).isoformat()}
    if kind in {EvidenceKind.UNDERLYING_QUOTE, EvidenceKind.FX}:
        base.update(symbol="AAPL" if kind is EvidenceKind.UNDERLYING_QUOTE else "GBPUSD",
                    bid="1.20", ask="1.21", bid_size=10, ask_size=10)
    if kind is EvidenceKind.FX: base["mid"] = "0.80"
    if kind is EvidenceKind.OPTION_QUOTE:
        base.update(symbol=CONTRACT, underlying="AAPL", expiration="2026-08-28", strike="20",
                    right="put", multiplier=100, currency="USD", market="US",
                    bid="1.00", ask="1.10", bid_size=10, ask_size=11)
    if kind is EvidenceKind.OPTION_CHAIN:
        base["contracts"] = [{"symbol": CONTRACT, "underlying": "AAPL", "expiration": "2026-08-28",
                              "strike": "20", "right": "put", "multiplier": 100}]
    return base


def packet(kind, *, timestamp=None, feed=None, **changes):
    n = normalized(kind, timestamp); n.update(changes)
    return ingest_response(kind=kind, provider="alpaca", feed=feed or ("OPRA" if kind in {EvidenceKind.OPTION_QUOTE, EvidenceKind.OPTION_CHAIN} else "IEX"),
                           request={"kind": kind.value}, requested_at=DECISION - timedelta(seconds=20),
                           received_at=(timestamp or DECISION - timedelta(seconds=10)) + timedelta(seconds=1),
                           raw={"alpaca": n}, normalized=n)


def bundle(): return {kind: packet(kind) for kind in EvidenceKind}


def run(tolerance=timedelta(minutes=5)):
    portfolio = PortfolioRisk(Decimal("100000"), Decimal("100000"), Decimal("100000"), {}, {})
    return PaperRun(DEFAULT_MANDATE, portfolio, tolerance=tolerance)


def option():
    return Instrument(CONTRACT, AssetType.OPTION, "Apple", "Technology", "US", underlying="AAPL",
                      expiry=date(2026, 8, 28), strike=Decimal("20"), right=OptionRight.PUT, multiplier=100)


def test_ingestion_and_loading_are_separate_and_import_never_reseals():
    original = packet(EvidenceKind.OPTION_QUOTE); imported = load_packet(original.as_json())
    assert imported == original and imported.verify()
    for field, value in [("packet_id", "arbitrary"), ("seal", "0" * 64), ("raw_sha256", "0" * 64),
                         ("provider", "evil"), ("received_at", original.received_at + timedelta(seconds=1))]:
        assert not replace(imported, **{field: value}).verify()
    assert not replace(imported, raw={"changed": True}).verify()
    assert not replace(imported, normalized={**imported.normalized, "ask": "9"}).verify()


def test_content_address_and_atomic_idempotent_append(tmp_path):
    p = packet(EvidenceKind.OPTION_QUOTE); store = AppendOnlyEvidenceStore(tmp_path / "packets.jsonl")
    assert store.append(p) and not store.append(p)
    forged = replace(p, normalized={**p.normalized, "ask": "7"})
    with pytest.raises(ValueError): store.append(forged)
    assert len((tmp_path / "packets.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("changes,reason", [({"ask": None}, "one-sided"), ({"ask_size": 0}, "zero-size"),
                                              ({"bid": "2", "ask": "1"}, "crossed")])
def test_quote_quarantine_lists_reasons(changes, reason):
    p = packet(EvidenceKind.OPTION_QUOTE, **changes)
    result = assess(p, as_of=DECISION, cutoff=DECISION, max_age=timedelta(seconds=60))
    assert result.verified and result.quarantined and not result.actionable and reason in result.reasons


def test_received_asof_staleness_and_cutoff_are_independent():
    p = packet(EvidenceKind.OPTION_QUOTE)
    result = assess(p, as_of=p.requested_at, cutoff=DECISION, max_age=timedelta(seconds=1))
    assert "received after as-of" in result.reasons and "future-dated" in result.reasons
    stale = assess(p, as_of=DECISION + timedelta(minutes=2), cutoff=DECISION + timedelta(minutes=2), max_age=timedelta(seconds=60))
    assert "stale" in stale.reasons


def test_missing_family_and_non_opra_or_contract_mismatch_fail_closed():
    evidence = bundle(); evidence.pop(EvidenceKind.DIVIDEND)
    assert "missing evidence family: dividend" in run().assess_bundle(evidence, as_of=DECISION, cutoff=DECISION).reasons
    r = run(); r.create_research(datetime(2026, 8, 7, 12, 33, tzinfo=UTC), (ResearchCandidate("AAPL", "value"),))
    evidence = bundle(); evidence[EvidenceKind.OPTION_QUOTE] = packet(EvidenceKind.OPTION_QUOTE, feed="IEX")
    with pytest.raises(ValueError, match="not OPRA"): r.decide(DECISION, Order("o", option(), Side.SELL, 1, "open"), evidence)
    evidence = bundle(); evidence[EvidenceKind.OPTION_QUOTE] = packet(EvidenceKind.OPTION_QUOTE, underlying="MSFT")
    with pytest.raises(ValueError, match="underlying mismatch"): r.decide(DECISION, Order("o", option(), Side.SELL, 1, "open"), evidence)


def test_executed_checks_no_boolean_bypass_and_separate_prospective_events():
    r = run(); research_at = datetime(2026, 8, 7, 12, 34, 30, tzinfo=UTC)
    research = r.create_research(research_at, (ResearchCandidate("AAPL", "value"),)); assert research.research_at == research_at
    decision = r.decide(DECISION, Order("o", option(), Side.SELL, 1, "open"), bundle())
    assert decision.verify() and r.rule_results.verify()
    assert "mandate_approved" not in inspect.signature(r.submit).parameters
    assert "risk_approved" not in inspect.signature(r.submit).parameters
    with pytest.raises(ValueError): r.submit(DECISION)
    submission = r.submit(DECISION + timedelta(seconds=1))
    observed = submission.submitted_at + timedelta(seconds=1)
    q = packet(EvidenceKind.OPTION_QUOTE, timestamp=observed)
    fill = r.simulate_fill(q, as_of=q.received_at + timedelta(seconds=1))
    assert fill["price"] == "1.00" and fill["decision_at"] < fill["submitted_at"] < fill["quote_at"] < fill["quote_available_at"] < fill["filled_at"]
    with pytest.raises(ValueError, match="observed before"):
        r.simulate_fill(packet(EvidenceKind.OPTION_QUOTE, timestamp=submission.submitted_at), as_of=DECISION + timedelta(seconds=5))


def test_tolerance_dst_and_latency():
    r = run(); actual = datetime(2026, 8, 7, 12, 35, tzinfo=UTC)
    assert r.create_research(actual, (ResearchCandidate("AAPL", "x"),)).research_at == actual
    with pytest.raises(ValueError, match="tolerance"):
        run().create_research(actual + timedelta(seconds=1), (ResearchCandidate("AAPL", "x"),))
    run(timedelta(minutes=8)).create_research(actual + timedelta(minutes=3), (ResearchCandidate("AAPL", "x"),))
    run().create_research(datetime(2026, 12, 7, 13, 35, tzinfo=UTC), (ResearchCandidate("AAPL", "x"),))


def test_replay_invariant_is_not_python_assert(tmp_path):
    source = "import inspect; from value_options.operations import PaperRun; assert 'assert ' not in inspect.getsource(PaperRun.excluded_replay)"
    env = {**os.environ, "PYTHONPATH": "src"}
    subprocess.run([sys.executable, "-O", "-c", source], check=True, env=env)
    report = run().excluded_replay(bundle(), as_of=DECISION)
    assert report.excluded and not report.actionable


def test_cli_complete_assessment_and_machine_report(tmp_path, capsys):
    operation = {"research_at": "2026-08-07T12:33:00+00:00", "decision_at": DECISION.isoformat(),
                 "submitted_at": (DECISION + timedelta(seconds=1)).isoformat(), "order_id": "cli-order",
                 "side": "sell", "quantity": 1, "intent": "open", "thesis": "value",
                 "instrument": {"symbol": CONTRACT, "issuer": "Apple", "sector": "Technology",
                                "underlying": "AAPL", "expiration": "2026-08-28", "strike": "20", "right": "put"}}
    path = tmp_path / "bundle.json"; path.write_text(json.dumps({"packets": [p.as_json() for p in bundle().values()], "operation": operation}))
    for command in ("dry-run", "replay"):
        output = tmp_path / f"{command}.json"
        assert main([command, str(path), "--as-of", DECISION.isoformat(), "--output", str(output)]) == 0
        result = json.loads(output.read_text())
        assert result["verified"] and (result["actionable"] if command == "dry-run" else result["excluded"])
    assert "PAPER ONLY | NO LIVE ORDER" in capsys.readouterr().out
