"""Deterministic US equity-option lifecycle and evidence quarantine rules."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from .accounting import LedgerEvent
from .calendar import MarketCalendarEvidence
from .models import AssetType, Instrument, MarketQuote, OptionRight, Order, require_utc


class LifecycleAction(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    PHYSICAL_ASSIGNMENT = "physical_assignment"
    RECONCILE_NEXT_BUSINESS_MORNING = "reconcile_next_business_morning"
    QUARANTINE = "quarantine"
    ASSIGN_NEXT_RECONCILIATION = "assign_next_reconciliation"


@dataclass(frozen=True)
class LifecycleDecision:
    action: LifecycleAction
    reason: str
    effective_date: date | None = None


def expiry_instruction(option: Instrument, spot: Decimal | None, evaluated_at: datetime,
                       market_close_at: datetime, evidence_complete: bool = True,
                       after_hours_pending: bool = False,
                       calendar_evidence: MarketCalendarEvidence | None = None) -> LifecycleDecision:
    """Close pin risk before expiry or physically assign at $0.01 ITM."""
    require_utc(evaluated_at, "evaluated_at")
    require_utc(market_close_at, "market_close_at")
    if option.adjusted or not option.occ_verified:
        return LifecycleDecision(LifecycleAction.QUARANTINE, "adjusted contract awaiting OCC verification")
    if not evidence_complete or spot is None:
        return LifecycleDecision(LifecycleAction.QUARANTINE, "required expiry evidence is missing")
    distance = abs(spot - option.strike) / option.strike
    if evaluated_at <= market_close_at and market_close_at - evaluated_at <= timedelta(hours=1) and distance <= Decimal("0.01"):
        return LifecycleDecision(LifecycleAction.CLOSE, "pin risk: within 1% of strike inside final hour")
    if after_hours_pending:
        next_session = (calendar_evidence.next_session_after(option.expiry, evaluated_at)
                        if calendar_evidence is not None else None)
        if next_session is None:
            return LifecycleDecision(LifecycleAction.QUARANTINE,
                                     "missing, stale or unverified market-calendar evidence")
        return LifecycleDecision(LifecycleAction.RECONCILE_NEXT_BUSINESS_MORNING,
                                 "after-hours expiry evidence pending",
                                 next_session)
    intrinsic = spot - option.strike if option.right is OptionRight.CALL else option.strike - spot
    if intrinsic >= Decimal("0.01"):
        return LifecycleDecision(LifecycleAction.PHYSICAL_ASSIGNMENT, "$0.01 or more ITM at expiry")
    return LifecycleDecision(LifecycleAction.HOLD, "expires out of the money; no cash settlement")


def assignment_events(event_prefix: str, option: Instrument, contracts: int, assigned_at: datetime,
                      fx_to_gbp: Decimal) -> tuple[LedgerEvent, LedgerEvent, LedgerEvent]:
    """Return physical share and strike-cash legs; never an option cash settlement."""
    require_utc(assigned_at, "assigned_at")
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    shares = contracts * option.multiplier
    share_delta = -shares if option.right is OptionRight.CALL else shares
    cash = option.strike * shares * fx_to_gbp
    cash_delta = cash if option.right is OptionRight.CALL else -cash
    underlying = Instrument(option.underlying, AssetType.EQUITY, option.issuer,
                            option.sector, option.market, option.is_etf)
    return (
        LedgerEvent(f"{event_prefix}:option", "option_assignment_close", assigned_at,
                    Decimal("0"), option, contracts, option.symbol),
        LedgerEvent(f"{event_prefix}:shares", "physical_assignment", assigned_at,
                    Decimal("0"), underlying, share_delta, option.symbol),
        LedgerEvent(f"{event_prefix}:cash", "assignment_cash", assigned_at,
                    cash_delta, None, 0, option.symbol),
    )


def ex_dividend_instruction(option: Instrument, spot: Decimal, extrinsic: Decimal,
                            dividend: Decimal, ex_date: date, today: date,
                            sale_floor: Decimal, prospectively_closed: bool = False) -> LifecycleDecision:
    if prospectively_closed:
        return LifecycleDecision(LifecycleAction.HOLD, "covered call was prospectively closed")
    if option.right is OptionRight.CALL and spot > option.strike and today < ex_date and dividend > extrinsic:
        if option.strike < sale_floor:
            return LifecycleDecision(LifecycleAction.CLOSE,
                                     "mandatory close: assumed assignment would breach sale floor")
        return LifecycleDecision(LifecycleAction.PHYSICAL_ASSIGNMENT,
                                 "assumed early assignment: dividend exceeds remaining extrinsic")
    return LifecycleDecision(LifecycleAction.HOLD, "ex-dividend early-assignment trigger not met")


def deep_itm_csp_instruction(option: Instrument, spot: Decimal, quote: MarketQuote,
                             today: date) -> LifecycleDecision:
    """Use the recorded ask (conservative close price) to calculate put extrinsic."""
    if quote.instrument != option:
        raise ValueError("market evidence does not match CSP")
    dte = (option.expiry - today).days
    intrinsic = max(Decimal("0"), option.strike - spot)
    assert quote.ask is not None  # MarketQuote rejects one-sided evidence.
    extrinsic = max(Decimal("0"), quote.ask - intrinsic)
    if option.right is OptionRight.PUT and intrinsic > 0 and 0 <= dte <= 5 and extrinsic <= Decimal("0.05"):
        return LifecycleDecision(LifecycleAction.ASSIGN_NEXT_RECONCILIATION,
                                 "ITM CSP at <=5 DTE with ask-based extrinsic <= $0.05")
    return LifecycleDecision(LifecycleAction.HOLD, "CSP assignment trigger not met")


def validate_roll(close_order: Order, open_order: Order) -> None:
    """A roll must be represented by two independently auditable orders."""
    if close_order.order_id == open_order.order_id or close_order.intent != "close" or open_order.intent != "open":
        raise ValueError("rolls require separate close and open trades")
    if close_order.instrument.underlying != open_order.instrument.underlying:
        raise ValueError("roll legs must share an underlying")
