from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from value_options.accounting import LedgerEvent, PaperLedger, Position, reconcile
from value_options.calendar import MarketCalendarEvidence
from value_options.execution import conservative_fill
from value_options.lifecycle import (
    LifecycleAction, assignment_events, deep_itm_csp_instruction, ex_dividend_instruction,
    expiry_instruction, validate_roll,
)
from value_options.mandate import DEFAULT_MANDATE
from value_options.models import (
    AssetType, Instrument, MarketQuote, Observation, OptionRight, Order, OrderSubmission,
    ResearchCandidate, ResearchPacket, Side, TradingDecision,
)
from value_options.risk import PortfolioRisk, RiskEngine, RiskViolation, drawdown_controls

UTC = timezone.utc
DAY = date(2026, 1, 5)
R130 = datetime(2026, 1, 5, 13, 30, tzinfo=UTC)
R140 = datetime(2026, 1, 5, 14, 40, tzinfo=UTC)
SHARE = Instrument("ACME", AssetType.EQUITY, "ACME", "Industrials", "US")
ETF = Instrument("WORLD", AssetType.EQUITY, "WORLD", "Diversified", "UK", True)


def option(right=OptionRight.PUT, strike="20", expiry=date(2026, 2, 20), adjusted=False):
    return Instrument(f"ACME-{right.value}-{strike}", AssetType.OPTION, "ACME", "Industrials", "US",
                      False, "ACME", expiry, Decimal(strike), right, 100, adjusted, not adjusted)


def order(i, side=Side.BUY, quantity=1, intent="open", **kwargs):
    return Order("order-1", i, side, quantity, intent, **kwargs)


def quote(i=SHARE, bid="9.90", ask="10.10", at=R140, available=None):
    return MarketQuote(i, "consolidated-feed", i.market, "USD", Decimal("0.80"),
                       None if bid is None else Decimal(bid), None if ask is None else Decimal(ask),
                       10, 12, at, available or at)


def portfolio(positions=None, marks=None, nav="100000", peak="100000", cash="100000"):
    return PortfolioRisk(Decimal(nav), Decimal(peak), Decimal(cash), positions or {}, marks or {})


def calendar(start, end, sessions, observed_at=R140, verified=True):
    return MarketCalendarEvidence("cal-1", "alpaca-read-only", "US", observed_at,
                                  start, end, tuple(sessions), verified).sealed()


def test_authoritative_capital_and_currency():
    assert DEFAULT_MANDATE.starting_cash == Decimal("100000")
    assert DEFAULT_MANDATE.base_currency == "GBP"


def test_research_packet_schedule_shortlist_and_hindsight():
    candidate = ResearchCandidate("ACME", "value thesis")
    packet = ResearchPacket("p1", "2026.2", R130, (candidate,),
                            (Observation("filing", "facts", R130),), "shortlist only").sealed()
    assert packet.verify()
    assert not replace(packet, rationale="hindsight edit").verify()
    with pytest.raises(ValueError, match="13:30"):
        replace(packet, research_at=R130 + timedelta(minutes=1), seal="")
    with pytest.raises(ValueError, match="hindsight"):
        ResearchPacket("p2", "2026.2", R130, (candidate,),
                       (Observation("future", "x", R140),), "invalid")


def test_london_cutoffs_convert_across_bst_and_gmt_and_block_market_open_data():
    candidate = ResearchCandidate("ACME", "value thesis")
    # 10 August 2026 is BST: 13:30/14:40 London are 12:30/13:40 UTC.
    bst_research = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
    bst_decision = datetime(2026, 8, 10, 13, 40, tzinfo=UTC)
    packet = ResearchPacket("bst", "2026.2", bst_research, (candidate,),
                            (Observation("pre-open", "known", bst_research),), "summer").sealed()
    assert packet.verify()
    TradingDecision("bst-d", "bst", bst_decision, order(option()), ()).sealed()
    with pytest.raises(ValueError, match="hindsight"):
        ResearchPacket("open", "2026.2", bst_research, (candidate,),
                       (Observation("subsequent market open", "future", bst_research + timedelta(minutes=1)),),
                       "must not leak")

    # 5 January 2026 is GMT, so the London cutoffs equal 13:30/14:40 UTC.
    ResearchPacket("gmt", "2026.2", R130, (candidate,), (), "winter").sealed()
    TradingDecision("gmt-d", "gmt", R140, order(option()), ()).sealed()
    with pytest.raises(ValueError, match="Europe/London"):
        ResearchPacket("fixed-utc", "2026.2", datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
                       (candidate,), (), "wrong summer UTC")


@pytest.mark.parametrize(("day", "research_utc", "decision_utc"), (
    (date(2026, 3, 27), (13, 30), (14, 40)),  # Friday before BST begins
    (date(2026, 3, 30), (12, 30), (13, 40)),  # Monday after BST begins
    (date(2026, 10, 23), (12, 30), (13, 40)), # Friday before GMT resumes
    (date(2026, 10, 26), (13, 30), (14, 40)), # Monday after GMT resumes
))
def test_london_cutoffs_at_bst_gmt_transitions(day, research_utc, decision_utc):
    candidate = ResearchCandidate("ACME", "transition evidence")
    research_at = datetime.combine(day, datetime.min.time(), UTC).replace(
        hour=research_utc[0], minute=research_utc[1])
    decision_at = datetime.combine(day, datetime.min.time(), UTC).replace(
        hour=decision_utc[0], minute=decision_utc[1])
    ResearchPacket(f"p-{day}", "2026.2", research_at, (candidate,), (), "timezone").sealed()
    TradingDecision(f"d-{day}", f"p-{day}", decision_at, order(option()), ()).sealed()


def test_1440_decision_and_prospective_submission_only():
    selected = order(option())
    decision = TradingDecision("d1", "p1", R140, selected,
                               (Observation("chain", "snapshot", R140),)).sealed()
    assert decision.verify()
    with pytest.raises(ValueError, match="hindsight"):
        replace(decision, observations=(Observation("late", "x", R140 + timedelta(seconds=1)),), seal="")
    with pytest.raises(ValueError, match="predate"):
        OrderSubmission("d1", R140, selected, R140 - timedelta(microseconds=1))


def test_quotes_reject_one_sided_crossed_zero_size_and_bad_market():
    with pytest.raises(ValueError, match="one-sided"):
        quote(bid=None)
    with pytest.raises(ValueError, match="crossed"):
        quote(bid="11", ask="10")
    with pytest.raises(ValueError, match="zero-size"):
        replace(quote(), bid_size=0)
    with pytest.raises(ValueError, match="matching market"):
        replace(quote(), market="UK")


def test_conservative_fill_timestamps_sides_and_staleness():
    submission = OrderSubmission("d1", R140, order(SHARE), R140)
    fill_at = R140 + timedelta(seconds=1)
    buy = conservative_fill("f1", submission, quote(at=R140), fill_at, 60)
    assert buy.price == Decimal("10.10") and buy.filled_at != buy.quote_market_at
    sell_submission = replace(submission, order=order(SHARE, Side.SELL))
    assert conservative_fill("f2", sell_submission, quote(at=R140), fill_at, 60).price == Decimal("9.90")
    with pytest.raises(ValueError, match="retrospective"):
        conservative_fill("f3", submission, quote(at=R140), R140 - timedelta(seconds=1), 60)
    with pytest.raises(ValueError, match="future"):
        conservative_fill("f4", submission, quote(at=R140 + timedelta(seconds=2)), fill_at, 60)
    with pytest.raises(ValueError, match="stale"):
        conservative_fill("f5", submission, quote(at=R140 - timedelta(seconds=61)), fill_at, 60)


def test_instrument_universe_and_opening_dte():
    with pytest.raises(ValueError, match="developed"):
        Instrument("EM", AssetType.EQUITY, "EM", "Finance", "BR")
    with pytest.raises(ValueError, match="US-listed"):
        replace(option(), market="UK")
    near = option(expiry=DAY + timedelta(days=13))
    with pytest.raises(RiskViolation, match="DTE"):
        RiskEngine(DEFAULT_MANDATE).check(order(near), portfolio(), DAY)


def test_short_shares_and_uncovered_calls_rejected():
    engine = RiskEngine(DEFAULT_MANDATE)
    with pytest.raises(RiskViolation, match="short shares"):
        engine.check(order(SHARE, Side.SELL), portfolio(), DAY)
    call = option(OptionRight.CALL, "12")
    with pytest.raises(RiskViolation, match="uncovered"):
        engine.check(order(call, Side.SELL, sale_floor=Decimal("10")), portfolio(), DAY)
    with pytest.raises(RiskViolation, match="spreads"):
        engine.check(order(call, Side.BUY), portfolio(), DAY)


def test_covered_call_fraction_exit_and_sale_floor():
    call = option(OptionRight.CALL, "12")
    p = portfolio({SHARE: Position(SHARE, 200, Decimal("1600"))}, {SHARE: Decimal("8"), call: Decimal("1")})
    engine = RiskEngine(DEFAULT_MANDATE)
    engine.check(order(call, Side.SELL, sale_floor=Decimal("10")), p, DAY)
    with pytest.raises(RiskViolation, match="fraction"):
        engine.check(order(call, Side.SELL, 2, sale_floor=Decimal("10")), p, DAY)
    engine.check(order(call, Side.SELL, 2, exit_entire_holding=True, sale_floor=Decimal("10")), p, DAY)
    with pytest.raises(RiskViolation, match="sale floor"):
        engine.check(order(call, Side.SELL, sale_floor=Decimal("13")), p, DAY)


def test_csp_security_collateral_drawdown_and_free_cash():
    put = option(strike="100")
    engine = RiskEngine(DEFAULT_MANDATE)
    engine.check(order(put, Side.SELL), portfolio(), DAY, Decimal("1"))
    with pytest.raises(RiskViolation, match="collateral"):
        engine.check(order(put, Side.SELL, 5), portfolio(), DAY, Decimal("1"))
    dd = portfolio(nav="89000", peak="100000", cash="89000")
    assert drawdown_controls(dd.drawdown, DEFAULT_MANDATE).csp_limit == Decimal("0.25")
    with pytest.raises(RiskViolation, match="collateral"):
        engine.check(order(option(strike="250"), Side.SELL), dd, DAY)
    low_cash = portfolio(cash="12000")
    with pytest.raises(RiskViolation, match="free cash"):
        engine.check(order(put, Side.SELL), low_cash, DAY)


def test_drawdown_tiers_are_deterministic():
    assert drawdown_controls(Decimal("0.0999"), DEFAULT_MANDATE).tier == 0
    assert drawdown_controls(Decimal("0.10"), DEFAULT_MANDATE).tier == 1
    assert drawdown_controls(Decimal("0.15"), DEFAULT_MANDATE).tier == 2
    assert drawdown_controls(Decimal("0.20"), DEFAULT_MANDATE).capital_preservation
    with pytest.raises(RiskViolation, match="drawdown"):
        RiskEngine(DEFAULT_MANDATE).check(order(SHARE), portfolio(nav="80000", peak="100000"), DAY)


def test_issuer_etf_sector_and_potential_exposure_limits():
    engine = RiskEngine(DEFAULT_MANDATE)
    expensive = portfolio(marks={SHARE: Decimal("110")})
    with pytest.raises(RiskViolation, match="issuer"):
        engine.check(order(SHARE, quantity=100), expensive, DAY)
    p_etf = portfolio(marks={ETF: Decimal("210")})
    with pytest.raises(RiskViolation, match="issuer"):
        engine.check(order(ETF, quantity=100), p_etf, DAY)

    one_contract = option(strike="140")
    engine.check(order(one_contract, Side.SELL), portfolio(), DAY)
    with pytest.raises(RiskViolation, match="issuer"):
        engine.check(order(option(strike="160"), Side.SELL), portfolio(), DAY)

    peer = Instrument("PEER", AssetType.EQUITY, "PEER", "Industrials", "US")
    sector_portfolio = portfolio({peer: Position(peer, 2000, Decimal("20000"))},
                                 {peer: Decimal("10"), SHARE: Decimal("51")})
    with pytest.raises(RiskViolation, match="sector"):
        engine.check(order(SHARE, quantity=100), sector_portfolio, DAY)

    positions, marks = {}, {}
    for index in range(9):
        holding = Instrument(f"H{index}", AssetType.EQUITY, f"ISSUER{index}", f"SECTOR{index}", "US")
        positions[holding] = Position(holding, 1000, Decimal("10000"))
        marks[holding] = Decimal("10")
    newcomer = Instrument("NEW", AssetType.EQUITY, "NEW", "NEW", "US")
    marks[newcomer] = Decimal("60")
    crowded = portfolio(positions, marks, cash="10000")
    with pytest.raises(RiskViolation, match="potential"):
        engine.check(order(newcomer, quantity=100), crowded, DAY)


def test_adjusted_contract_frozen_and_rolls_are_two_trades():
    with pytest.raises(RiskViolation, match="OCC"):
        RiskEngine(DEFAULT_MANDATE).check(order(option(adjusted=True)), portfolio(), DAY)
    old, new = option(expiry=date(2026, 2, 20)), option(expiry=date(2026, 3, 20))
    validate_roll(order(old, intent="close"), replace(order(new), order_id="order-2"))
    with pytest.raises(ValueError, match="separate"):
        validate_roll(order(old, intent="close"), order(new))


def test_pin_risk_assignment_after_hours_and_quarantine():
    put = option(strike="20", expiry=DAY)
    close = datetime(2026, 1, 5, 21, tzinfo=UTC)
    assert expiry_instruction(put, Decimal("20.10"), close - timedelta(minutes=30), close).action is LifecycleAction.CLOSE
    assert expiry_instruction(put, Decimal("19.99"), close + timedelta(minutes=1), close).action is LifecycleAction.PHYSICAL_ASSIGNMENT
    evidence = calendar(DAY, DAY + timedelta(days=7), (DAY + timedelta(days=1),), close)
    pending = expiry_instruction(put, Decimal("19"), close, close, after_hours_pending=True,
                                 calendar_evidence=evidence)
    assert pending.action is LifecycleAction.RECONCILE_NEXT_BUSINESS_MORNING
    assert pending.effective_date == date(2026, 1, 6)
    assert expiry_instruction(put, None, close, close, evidence_complete=False).action is LifecycleAction.QUARANTINE


def test_calendar_evidence_handles_us_holiday_and_normal_weekend():
    close = datetime(2026, 7, 3, 21, tzinfo=UTC)
    holiday_expiry = option(expiry=date(2026, 7, 3))
    # Stored US evidence omits the observed Independence Day closure and weekend.
    holiday_calendar = calendar(date(2026, 7, 3), date(2026, 7, 10),
                                (date(2026, 7, 6), date(2026, 7, 7)), close)
    holiday = expiry_instruction(holiday_expiry, Decimal("18"), close, close,
                                 after_hours_pending=True, calendar_evidence=holiday_calendar)
    assert holiday.effective_date == date(2026, 7, 6)

    friday = date(2026, 8, 7)
    weekend_expiry = option(expiry=friday)
    weekend_close = datetime(2026, 8, 7, 21, tzinfo=UTC)
    weekend_calendar = calendar(friday, date(2026, 8, 14), (date(2026, 8, 10),), weekend_close)
    weekend = expiry_instruction(weekend_expiry, Decimal("18"), weekend_close, weekend_close,
                                 after_hours_pending=True, calendar_evidence=weekend_calendar)
    assert weekend.effective_date == date(2026, 8, 10)


def test_calendar_evidence_missing_stale_or_unverified_quarantines():
    close = datetime(2026, 1, 5, 21, tzinfo=UTC)
    put = option(expiry=DAY)
    missing = expiry_instruction(put, Decimal("18"), close, close, after_hours_pending=True)
    assert missing.action is LifecycleAction.QUARANTINE
    stale = calendar(DAY - timedelta(days=7), DAY, (DAY,), close)
    assert expiry_instruction(put, Decimal("18"), close, close, True, True, stale).action is LifecycleAction.QUARANTINE
    unverified = calendar(DAY, DAY + timedelta(days=7), (DAY + timedelta(days=1),), close, False)
    assert expiry_instruction(put, Decimal("18"), close, close, True, True, unverified).action is LifecycleAction.QUARANTINE
    tampered = replace(calendar(DAY, DAY + timedelta(days=7), (DAY + timedelta(days=1),), close),
                       source="not-the-sealed-source")
    assert expiry_instruction(put, Decimal("18"), close, close, True, True, tampered).action is LifecycleAction.QUARANTINE


def test_physical_assignment_has_share_and_cash_legs_not_cash_settlement():
    put = option(strike="20", expiry=DAY)
    events = assignment_events("assign-1", put, 1, R140, Decimal("0.8"))
    assert events[0].quantity_delta == 1  # closes the short option
    assert events[1].quantity_delta == 100
    assert events[2].amount_gbp == Decimal("-1600.0")
    assert {e.kind for e in events} == {"option_assignment_close", "physical_assignment", "assignment_cash"}


def test_ex_dividend_and_deep_itm_rules():
    call = option(OptionRight.CALL, "20")
    assigned = ex_dividend_instruction(call, Decimal("22"), Decimal("0.20"), Decimal("0.30"),
                                       DAY + timedelta(days=1), DAY, Decimal("20"))
    assert assigned.action is LifecycleAction.PHYSICAL_ASSIGNMENT
    mandatory_close = ex_dividend_instruction(call, Decimal("22"), Decimal("0.20"), Decimal("0.30"),
                                              DAY + timedelta(days=1), DAY, Decimal("21"))
    assert mandatory_close.action is LifecycleAction.CLOSE
    already_closed = ex_dividend_instruction(call, Decimal("22"), Decimal("0.20"), Decimal("0.30"),
                                             DAY + timedelta(days=1), DAY, Decimal("21"), True)
    assert already_closed.action is LifecycleAction.HOLD


def test_csp_assignment_uses_ask_based_extrinsic_boundaries():
    put = option(strike="20", expiry=DAY + timedelta(days=5))
    # Intrinsic is $2.00; the actionable ask of $2.05 gives exactly $0.05 extrinsic.
    boundary_quote = quote(put, bid="2.00", ask="2.05")
    assert deep_itm_csp_instruction(put, Decimal("18"), boundary_quote, DAY).action is LifecycleAction.ASSIGN_NEXT_RECONCILIATION
    above = quote(put, bid="2.00", ask="2.051")
    assert deep_itm_csp_instruction(put, Decimal("18"), above, DAY).action is LifecycleAction.HOLD
    not_itm = quote(put, bid="0.01", ask="0.05")
    assert deep_itm_csp_instruction(put, Decimal("20"), not_itm, DAY).action is LifecycleAction.HOLD
    six_days = replace(put, expiry=DAY + timedelta(days=6))
    assert deep_itm_csp_instruction(six_days, Decimal("18"), quote(six_days, bid="2", ask="2.05"), DAY).action is LifecycleAction.HOLD


def test_append_only_idempotent_replay_and_reconciliation():
    ledger = PaperLedger(Decimal("100000"))
    event = LedgerEvent("deposit-1", "adjustment", R140, Decimal("10"), memo="approved fixture")
    assert ledger.append(event) and not ledger.append(event)
    assert ledger.cash == Decimal("100010")
    replayed = PaperLedger(Decimal("100000"))
    replayed.append(event)
    assert replayed.replay() == ledger.replay()
    assert reconcile(ledger.events, replayed.events) == ()
    missing = reconcile(ledger.events, ())
    assert missing[0].reason == "missing"
