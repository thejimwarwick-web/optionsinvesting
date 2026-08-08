# Value & Options Paper Fund — deterministic control foundation

This repository is an offline, standard-library-only paper-fund control plane. It
does **not** connect to Google Sheets or Alpaca, contain credentials, expose broker
order methods, use margin, or submit live trades.

## Implemented mandate

* £100,000 GBP starting NAV; shares and mainstream equity ETFs are restricted to
  an explicit developed-market set, while standard options must be US listed.
* New option shorts are accepted only as a cash-secured put or covered call.
  Short shares, naked options, speculative long options, spreads, adjusted options
  without OCC verification, leverage, and unrecognised derivatives are rejected.
* New options require at least 14 DTE. Normal issuer exposure is 10%, the
  one-contract exception is 15%, ETFs are 20%, and sector exposure is 25%.
* CSP collateral is capped at 40%, falling to 25% after a 10% drawdown. Free cash
  is at least 5% and maximum potential exposure is 95%.
* Covered calls are limited to 50% of shares. Covering 100% requires an explicit
  whole-holding exit flag, and every call strike must meet its recorded sale floor.
* Drawdown tiers are deterministic: at 10% CSP capacity tightens; at 15% new risk
  stops; at 20% capital-preservation status is asserted and new risk remains off.

## Time and evidence controls

The sealed 13:30 **Europe/London** research packet contains typed underlying
candidates, never an option contract. The sealed 14:40 Europe/London decision
selects the contract and rejects any observation not available by that cutoff.
The IANA timezone conversion is daylight-saving aware: on 10 August 2026 the two
cutoffs are 12:30 and 13:40 UTC, while winter GMT cutoffs retain the local clock
times. Decision, submission, quote-market,
quote-availability, and fill timestamps are separate. Submissions cannot predate
decisions and fills cannot predate submissions. Quotes record source, market,
currency/GBP conversion, bid/ask, sizes, and both timestamps. One-sided, crossed,
zero-size, stale, future, and market-mismatched quotes are rejected. Buys fill at
the ask and sells fill at the bid.

## Lifecycle and ledger

US equity options are never generically cash settled. The lifecycle module applies
$0.01 ITM physical assignment, final-hour pin closure within 1% of strike,
next-open-session after-hours reconciliation, covered-call sale-floor and
ex-dividend protection, evidence-based CSP assignment, separate roll orders, adjusted-contract
freezing, and evidence quarantine. Assignment emits separate option-close, physical
share, and strike-cash events.

For an ITM CSP with five or fewer calendar days remaining, put extrinsic value is
calculated as **max(0, recorded actionable ask minus intrinsic value)**. The ask is used
because buying at the ask is the conservative cost to close a short put. Extrinsic
of no more than $0.05 produces assignment at the next reconciliation. Before an
ex-dividend date, an ITM covered call is assumed assigned when the dividend exceeds
remaining extrinsic unless it was prospectively closed; closure is mandatory when
assignment at strike would breach its recorded sale floor.

Accounting is append-only and idempotent by event ID. Pure replay reconstructs the
same state, and reconciliation reports missing, unexpected, or mismatched records.
`AppendOnlyLedgerSink` is only a port for a future Google Sheets adapter.
`ReadOnlyAlpaca` deliberately exposes reads only.

Exchange sessions are never inferred from weekdays. Live and replay processing
require a verified, sealed `MarketCalendarEvidence` observation whose coverage
contains the event and a subsequent open session. The read-only evidence port is
suitable for persisting an Alpaca calendar response. Missing, future-dated,
out-of-coverage, unverified, or seal-invalid evidence quarantines the event.

## Deliberate limitations

* No live ledger or broker adapter is implemented.
* No calendar downloader or holiday database is embedded. An operator must store
  authoritative calendar evidence before lifecycle processing can proceed.
* Corporate-action terms remain quarantined until an external OCC-verification
  adapter supplies evidence.
* Initial provider-response ingestion is separate from packet loading. Ingestion
  creates a content-addressed ID and seals exactly once; loading preserves supplied
  hashes and seals for verification and cannot silently reseal tampered evidence.
  Atomic locked JSONL replacement prevents partial writes and rejects ID collisions.
  The dependency-injected read port deliberately contains no network client,
  credentials, broker submission, or Google Sheets writer.
* SHA-256 content addressing and seals provide deterministic tamper evidence only.
  They do **not** prove wall-clock creation time, authorship, or that an artifact
  existed before later evidence was seen. Locally generated research, decision,
  submission, and fill artifacts therefore declare `externally_attested: false`
  and `launch_eligible: false`. External time/identity attestation is reserved for
  a future append-only ledger adapter; local paper artifacts can never authorise
  launch or be presented as externally time-proven.
* Clock/calendar, underlying and option quotes/chains, corporate actions,
  dividends, and GBP/USD are accepted as evidence kinds, but never synthesized.
  Invalid, stale, future, mismatched, crossed, zero-size, or late evidence fails
  closed. Corporate-action terms still require an external authoritative verifier.
  Underlying and option quotes use the mandate quote-age limit. Calendar evidence
  instead has to cover the actual trading session. Corporate-action and dividend
  records require both an effective date and a retrieval timestamp, but are not
  incorrectly subjected to a five-minute quote timeout. FX bid/ask and midpoint
  must be finite and positive; FX size is optional because spot providers may not
  publish it, while stock and option exchange sizes remain mandatory and positive.
* Historical replay is explicitly excluded and cannot mutate launch status, cash,
  NAV, orders, or positions. Taxes, fees, and slippage beyond adverse-side fills
  are not accrued.

## Offline sequential operational runs

The operational path is deliberately three separate invocations. `research-run`
seals its underlying-only input before option information is accepted;
`decision-run` verifies that pre-existing artifact and writes distinct decision
and submission artifacts; `fill-run` accepts only a separate post-submission OPRA
quote. Multiple quote observations are retained and the latest available packet
is selected deterministically; duplicate non-quote families are quarantined as
ambiguous. Missing or malformed input produces a structured quarantine artifact.
`replay` accepts combined historical bundles only as explicitly excluded,
state-neutral test convenience. Every output says `PAPER ONLY` and `NO LIVE ORDER`:

```bash
value-options research-run research-input.json --at 2026-08-07T12:33:00Z --output artifacts/research.json
value-options decision-run artifacts/research.json decision-evidence.json --at 2026-08-07T13:42:00Z --submitted-at 2026-08-07T13:42:01Z --decision-output artifacts/decision.json --submission-output artifacts/submission.json
value-options fill-run artifacts/decision.json artifacts/submission.json post-submission-option-quote.json --as-of 2026-08-07T13:42:05Z --output artifacts/fill.json
value-options replay evidence-bundle.json --as-of 2026-08-07T13:40:30Z --output artifacts/replay.json
value-options inspect tests/fixtures/alpaca_opra_quote.json --as-of 2026-08-07T13:40:03Z --output artifacts/inspection.json
```

Operational commands exit non-zero whenever a research, decision, submission, or
fill stage is quarantined, invalid, or non-actionable, so schedulers cannot mistake
a written quarantine report for success. A valid inspection and a successfully
excluded replay retain a zero exit status.

## Tests

```bash
python -m pytest
python -m compileall -q src tests
git diff --check
```

CI runs these checks on Python 3.11, 3.12, and 3.13. The machine-readable
`tests/mandate_conformance.json` maps every approved rule to passing and
rejection/boundary coverage.
