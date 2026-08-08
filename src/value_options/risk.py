"""Authoritative pre-trade controls; option spreads never provide security."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .accounting import LedgerState, Position
from .mandate import FundMandate
from .models import AssetType, Instrument, OptionRight, Order, Side


class RiskViolation(ValueError):
    pass


@dataclass(frozen=True)
class PortfolioRisk:
    nav_gbp: Decimal
    peak_nav_gbp: Decimal
    cash_gbp: Decimal
    positions: dict[Instrument, Position]
    marks_gbp: dict[Instrument, Decimal]

    @property
    def drawdown(self) -> Decimal:
        return max(Decimal("0"), (self.peak_nav_gbp - self.nav_gbp) / self.peak_nav_gbp)


@dataclass(frozen=True)
class DrawdownControls:
    tier: int
    csp_limit: Decimal
    new_risk_allowed: bool
    capital_preservation: bool


def drawdown_controls(drawdown: Decimal, mandate: FundMandate) -> DrawdownControls:
    if drawdown >= Decimal("0.20"):
        return DrawdownControls(3, mandate.drawdown_csp_limit, False, True)
    if drawdown >= Decimal("0.15"):
        return DrawdownControls(2, mandate.drawdown_csp_limit, False, False)
    if drawdown >= Decimal("0.10"):
        return DrawdownControls(1, mandate.drawdown_csp_limit, True, False)
    return DrawdownControls(0, mandate.csp_collateral_limit, True, False)


class RiskEngine:
    def __init__(self, mandate: FundMandate) -> None:
        self.mandate = mandate

    def check(self, order: Order, portfolio: PortfolioRisk, on_date: date, fx_to_gbp: Decimal = Decimal("1")) -> None:
        i = order.instrument
        if i.adjusted or not i.occ_verified:
            raise RiskViolation("adjusted contract frozen pending OCC verification")
        controls = drawdown_controls(portfolio.drawdown, self.mandate)
        if order.intent == "open" and not controls.new_risk_allowed:
            raise RiskViolation("drawdown tier prohibits new risk")
        if i.asset_type is AssetType.OPTION and (i.expiry - on_date).days < self.mandate.minimum_opening_dte and order.intent == "open":
            raise RiskViolation("option opening DTE is below 14 days")
        current = portfolio.positions.get(i, Position(i)).quantity
        if i.asset_type is AssetType.OPTION and order.side is Side.BUY and order.intent == "open":
            raise RiskViolation("long-option speculation and spreads are prohibited")
        if i.asset_type is AssetType.EQUITY and order.side is Side.SELL and order.quantity > current:
            raise RiskViolation("short shares are prohibited")

        secured = Decimal("0")
        if i.asset_type is AssetType.OPTION and order.side is Side.SELL and order.intent == "open":
            if i.right is OptionRight.PUT:
                secured = i.strike * i.multiplier * order.quantity * fx_to_gbp
                existing = self._csp_collateral(portfolio, fx_to_gbp)
                if existing + secured > portfolio.nav_gbp * controls.csp_limit:
                    raise RiskViolation("cash-secured-put collateral limit exceeded")
            elif i.right is OptionRight.CALL:
                shares = self._underlying_shares(i, portfolio)
                already_covered = self._covered_call_shares(i, portfolio)
                maximum = shares if order.exit_entire_holding else int(shares * self.mandate.covered_call_fraction)
                if already_covered + order.quantity * i.multiplier > maximum:
                    raise RiskViolation("uncovered call or covered-call fraction exceeded")
                if order.sale_floor is None or i.strike < order.sale_floor:
                    raise RiskViolation("covered-call strike is below the approved sale floor")
            else:
                raise RiskViolation("naked option is prohibited")

        # Long options cannot be used to secure another leg: every short is checked
        # solely against cash or shares, which rejects spreads by construction.
        mark = portfolio.marks_gbp.get(i, Decimal("0"))
        trade_exposure = (i.strike * i.multiplier * order.quantity * fx_to_gbp
                          if i.asset_type is AssetType.OPTION and i.right is OptionRight.PUT and order.side is Side.SELL
                          else mark * i.multiplier * order.quantity)
        issuer_exposure = self._exposure(portfolio, lambda x: x.issuer == i.issuer) + trade_exposure
        issuer_limit = self.mandate.etf_limit if i.is_etf else self.mandate.issuer_limit
        if (i.asset_type is AssetType.OPTION and order.quantity == 1 and not i.is_etf):
            issuer_limit = self.mandate.one_contract_exception
        if issuer_exposure > portfolio.nav_gbp * issuer_limit:
            raise RiskViolation("issuer exposure limit exceeded")
        if self._exposure(portfolio, lambda x: x.sector == i.sector) + trade_exposure > portfolio.nav_gbp * self.mandate.sector_limit:
            raise RiskViolation("sector exposure limit exceeded")
        potential = self._exposure(portfolio, lambda _: True) + trade_exposure
        if potential > portfolio.nav_gbp * self.mandate.maximum_potential_exposure:
            raise RiskViolation("maximum potential exposure exceeded")
        purchase_cash = trade_exposure if order.side is Side.BUY and order.intent == "open" else Decimal("0")
        if portfolio.cash_gbp - secured - purchase_cash < portfolio.nav_gbp * self.mandate.minimum_free_cash:
            raise RiskViolation("minimum free cash would be breached")

    @staticmethod
    def _exposure(p: PortfolioRisk, predicate) -> Decimal:
        return sum((abs(pos.quantity) * i.multiplier * p.marks_gbp.get(i, Decimal("0"))
                    for i, pos in p.positions.items() if predicate(i)), Decimal("0"))

    @staticmethod
    def _underlying_shares(option: Instrument, p: PortfolioRisk) -> int:
        return sum(pos.quantity for i, pos in p.positions.items()
                   if i.asset_type is AssetType.EQUITY and i.symbol == option.underlying)

    @staticmethod
    def _covered_call_shares(option: Instrument, p: PortfolioRisk) -> int:
        return sum(abs(pos.quantity) * i.multiplier for i, pos in p.positions.items()
                   if i.asset_type is AssetType.OPTION and i.underlying == option.underlying
                   and i.right is OptionRight.CALL and pos.quantity < 0)

    @staticmethod
    def _csp_collateral(p: PortfolioRisk, fx: Decimal) -> Decimal:
        return sum(abs(pos.quantity) * i.multiplier * i.strike * fx for i, pos in p.positions.items()
                   if i.asset_type is AssetType.OPTION and i.right is OptionRight.PUT and pos.quantity < 0)
