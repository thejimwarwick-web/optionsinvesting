"""Fail-closed, sealed market-calendar evidence for live and replay use."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from .models import SealedRecord, require_utc


@dataclass(frozen=True)
class MarketCalendarEvidence(SealedRecord):
    """Stored observations, suitable for Alpaca's read-only calendar response."""

    evidence_id: str
    source: str
    market: str
    observed_at: datetime
    coverage_start: date
    coverage_end: date
    open_sessions: tuple[date, ...]
    verified: bool
    seal: str = ""

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "calendar.observed_at")
        if not self.evidence_id or not self.source or not self.market:
            raise ValueError("calendar evidence identity, source and market are required")
        if self.coverage_end < self.coverage_start:
            raise ValueError("calendar evidence coverage is inverted")
        if tuple(sorted(set(self.open_sessions))) != self.open_sessions:
            raise ValueError("open sessions must be unique and sorted")
        if any(day < self.coverage_start or day > self.coverage_end for day in self.open_sessions):
            raise ValueError("open session is outside evidence coverage")

    def next_session_after(self, day: date, replay_as_of: datetime) -> date | None:
        """Return a session only from verified, sealed, timely, covering evidence."""
        require_utc(replay_as_of, "replay_as_of")
        if not self.verified or not self.verify() or self.observed_at > replay_as_of:
            return None
        if day < self.coverage_start or day >= self.coverage_end:
            return None
        return next((session for session in self.open_sessions if session > day), None)


class ReadOnlyCalendarEvidenceSource(Protocol):
    """Read-only port; implementations may store Alpaca calendar observations."""

    def calendar_evidence(self, market: str, start: date, end: date) -> MarketCalendarEvidence: ...
