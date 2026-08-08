"""Append-only, replayable accounting designed for a future Sheets adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Protocol

from .execution import Fill
from .models import Instrument, Side, require_utc


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    kind: str
    occurred_at: datetime
    amount_gbp: Decimal
    instrument: Instrument | None = None
    quantity_delta: int = 0
    memo: str = ""

    def __post_init__(self) -> None:
        require_utc(self.occurred_at, "occurred_at")
        if not self.event_id:
            raise ValueError("event id is required")


class AppendOnlyLedgerSink(Protocol):
    """Port for a later Google Sheets implementation; this package has none."""

    def append_if_absent(self, event: LedgerEvent) -> bool: ...
    def read_all(self) -> Iterable[LedgerEvent]: ...


@dataclass
class Position:
    instrument: Instrument
    quantity: int = 0
    cost_basis_gbp: Decimal = Decimal("0")


class PaperLedger:
    def __init__(self, starting_cash: Decimal) -> None:
        self.starting_cash = starting_cash
        self.events: list[LedgerEvent] = []
        self._event_ids: set[str] = set()

    def append(self, event: LedgerEvent) -> bool:
        """Append once. Replaying the same event id is a deterministic no-op."""
        if event.event_id in self._event_ids:
            return False
        self._event_ids.add(event.event_id)
        self.events.append(event)
        return True

    def book_fill(self, fill: Fill, instrument: Instrument, secured_option_short: bool = False) -> bool:
        sign = Decimal("-1") if fill.side is Side.BUY else Decimal("1")
        quantity = fill.quantity if fill.side is Side.BUY else -fill.quantity
        event = LedgerEvent(f"fill:{fill.fill_id}", "fill", fill.filled_at,
                            sign * fill.cash_value, instrument, quantity, fill.order_id)
        projected = self.replay((*self.events, event))
        projected_quantity = projected.positions.get(instrument, Position(instrument)).quantity
        permitted_short = secured_option_short and instrument.asset_type.value == "option"
        if projected.cash < 0 or (projected_quantity < 0 and not permitted_short):
            raise ValueError("margin, leverage and short shares/options are prohibited in accounting")
        return self.append(event)

    def replay(self, events: Iterable[LedgerEvent] | None = None) -> LedgerState:
        cash = self.starting_cash
        positions: dict[Instrument, Position] = {}
        seen: set[str] = set()
        for event in self.events if events is None else events:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            cash += event.amount_gbp
            if event.instrument and event.quantity_delta:
                p = positions.setdefault(event.instrument, Position(event.instrument))
                p.quantity += event.quantity_delta
                if event.quantity_delta > 0:
                    p.cost_basis_gbp += -event.amount_gbp
                elif p.quantity + -event.quantity_delta:
                    old_quantity = p.quantity - event.quantity_delta
                    p.cost_basis_gbp *= Decimal(p.quantity) / Decimal(old_quantity)
                if p.quantity == 0:
                    positions.pop(event.instrument)
        return LedgerState(cash, positions)

    @property
    def cash(self) -> Decimal:
        return self.replay().cash


@dataclass(frozen=True)
class LedgerState:
    cash: Decimal
    positions: dict[Instrument, Position]


@dataclass(frozen=True)
class ReconciliationDifference:
    event_id: str
    reason: str


def reconcile(expected: Iterable[LedgerEvent], actual: Iterable[LedgerEvent]) -> tuple[ReconciliationDifference, ...]:
    expected_by_id = {e.event_id: e for e in expected}
    actual_by_id = {e.event_id: e for e in actual}
    differences = []
    for event_id in sorted(expected_by_id.keys() | actual_by_id.keys()):
        if event_id not in actual_by_id:
            differences.append(ReconciliationDifference(event_id, "missing"))
        elif event_id not in expected_by_id:
            differences.append(ReconciliationDifference(event_id, "unexpected"))
        elif expected_by_id[event_id] != actual_by_id[event_id]:
            differences.append(ReconciliationDifference(event_id, "mismatch"))
    return tuple(differences)
