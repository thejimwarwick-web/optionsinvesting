"""The authoritative paper-fund mandate, encoded as immutable policy."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FundMandate:
    version: str = "2026.2"
    base_currency: str = "GBP"
    starting_cash: Decimal = Decimal("100000")
    issuer_limit: Decimal = Decimal("0.10")
    one_contract_exception: Decimal = Decimal("0.15")
    etf_limit: Decimal = Decimal("0.20")
    sector_limit: Decimal = Decimal("0.25")
    csp_collateral_limit: Decimal = Decimal("0.40")
    drawdown_csp_limit: Decimal = Decimal("0.25")
    minimum_free_cash: Decimal = Decimal("0.05")
    maximum_potential_exposure: Decimal = Decimal("0.95")
    covered_call_fraction: Decimal = Decimal("0.50")
    minimum_opening_dte: int = 14
    max_quote_age_seconds: int = 60


DEFAULT_MANDATE = FundMandate()
