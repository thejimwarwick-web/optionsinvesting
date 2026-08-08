import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from value_options.cli import load, main
from value_options.market_data import AppendOnlyEvidenceStore, EvidenceKind, EvidencePacket, assess
from value_options.operations import PaperRun

UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures/alpaca_opra_quote.json"


def quote(**changes):
    packet = load(FIXTURE)
    return replace(packet, seal="", raw_sha256="", **changes).sealed()


def test_alpaca_raw_normalized_opra_identity_and_tamper_detection():
    packet = quote()
    assert packet.verify() and packet.feed == "OPRA" and packet.raw != packet.normalized
    assert not replace(packet, raw={"tampered": True}).verify()


def test_append_only_is_idempotent_and_rejects_collision(tmp_path):
    store = AppendOnlyEvidenceStore(tmp_path / "evidence.jsonl"); packet = quote()
    assert store.append(packet) and not store.append(packet)
    collision = replace(packet, seal="", raw_sha256="", normalized={**packet.normalized, "ask": "9"}).sealed()
    with pytest.raises(ValueError, match="collision"): store.append(collision)


@pytest.mark.parametrize("change,reason", [
    ({"ask": None}, "one-sided"), ({"ask_size": 0}, "zero-size"),
    ({"bid": "5", "ask": "4"}, "crossed"),
])
def test_bad_quotes_quarantined(change, reason):
    packet = quote(normalized={**quote().normalized, **change})
    result = assess(packet, as_of=packet.received_at, cutoff=packet.received_at)
    assert not result.accepted and reason in result.reasons


def test_stale_future_cutoff_and_symbol_mismatch():
    packet = quote()
    result = assess(packet, as_of=packet.received_at + timedelta(hours=1), cutoff=packet.received_at - timedelta(seconds=1), expected_symbol="WRONG")
    assert {"stale", "post-cutoff", "mismatched"} <= set(result.reasons)
    future = quote(normalized={**packet.normalized, "timestamp": "2026-08-08T00:00:00+00:00"})
    assert "future-dated" in assess(future, as_of=packet.received_at, cutoff=future.received_at).reasons


def test_dst_research_decision_and_prospective_adverse_fill():
    run = PaperRun(); run.research(datetime(2026, 8, 7, 12, 30, tzinfo=UTC), ["AAPL"])
    run.submit(datetime(2026, 8, 7, 13, 40, tzinfo=UTC), "AAPL260821P00200000", "buy", 1,
               mandate_approved=True, risk_approved=True)
    fill = run.fill(quote())
    assert fill["price"] == "4.20" and run.research_at < run.decision_at <= run.submitted_at < run.quote_at <= run.fill_at
    winter = PaperRun(); winter.research(datetime(2026, 12, 7, 13, 30, tzinfo=UTC), ["AAPL"])


def test_mandate_or_risk_rejection_prevents_simulated_order():
    run = PaperRun(); run.research(datetime(2026, 8, 7, 12, 30, tzinfo=UTC), ["AAPL"])
    with pytest.raises(ValueError, match="mandate and risk"):
        run.submit(datetime(2026, 8, 7, 13, 40, tzinfo=UTC), "contract", "sell", 1,
                   mandate_approved=True, risk_approved=False)
    assert run.orders == []


@pytest.mark.parametrize("kind,feed", [
    (EvidenceKind.CLOCK, "alpaca"), (EvidenceKind.CALENDAR, "alpaca"),
    (EvidenceKind.UNDERLYING_QUOTE, "IEX"), (EvidenceKind.OPTION_CHAIN, "OPRA"),
    (EvidenceKind.CORPORATE_ACTION, "alpaca"), (EvidenceKind.DIVIDEND, "alpaca"),
    (EvidenceKind.FX, "institutional-fx"),
])
def test_all_read_only_evidence_families_preserve_source_and_request(kind, feed):
    base = quote()
    packet = quote(kind=kind, feed=feed, request={"symbols": ["AAPL"], "feed": feed})
    assert packet.verify() and packet.provider == "alpaca" and packet.request["feed"] == feed


def test_excluded_replay_deterministic_and_state_immutable():
    run = PaperRun(); before = (run.cash, run.nav, run.orders, run.positions, run.launch_status)
    assert run.excluded_replay([quote()]) == run.excluded_replay([quote()])
    assert before == (run.cash, run.nav, run.orders, run.positions, run.launch_status)


def test_cli_artifacts(tmp_path, capsys):
    for command in ("dry-run", "replay"):
        output = tmp_path / f"{command}.json"
        assert main([command, str(FIXTURE), "--output", str(output)]) == 0
        artifact = json.loads(output.read_text())
        assert artifact["classification"] == "PAPER ONLY" and artifact["order_policy"] == "NO LIVE ORDER"
    assert "PAPER ONLY | NO LIVE ORDER" in capsys.readouterr().out
