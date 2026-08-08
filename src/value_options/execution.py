"""Prospective conservative paper execution only."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import MarketQuote, OrderSubmission, Side, require_utc


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    instrument_symbol: str
    side: Side
    quantity: int
    multiplier: int
    price: Decimal
    filled_at: datetime
    quote_market_at: datetime
    quote_available_at: datetime
    fx_to_gbp: Decimal

    @property
    def cash_value(self) -> Decimal:
        return self.price * self.quantity * self.multiplier * self.fx_to_gbp


def conservative_fill(
    fill_id: str,
    submission: OrderSubmission,
    quote: MarketQuote,
    filled_at: datetime,
    max_age_seconds: int,
) -> Fill:
    require_utc(filled_at, "filled_at")
    order = submission.order
    if quote.instrument != order.instrument:
        raise ValueError("quote does not match order instrument")
    if filled_at < submission.submitted_at:
        raise ValueError("retrospective fill is prohibited")
    if quote.market_at > filled_at or quote.available_at > filled_at:
        raise ValueError("future quote is prohibited")
    if (filled_at - quote.market_at).total_seconds() > max_age_seconds:
        raise ValueError("quote is stale")
    price = quote.ask if order.side is Side.BUY else quote.bid
    return Fill(fill_id, order.order_id, order.instrument.symbol, order.side, order.quantity,
                order.instrument.multiplier, price, filled_at, quote.market_at, quote.available_at,
                quote.fx_to_gbp)
