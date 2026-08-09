# Value & Options Paper Fund — deterministic control foundation

This repository is an offline-by-default paper-fund control plane. Optional,
explicitly activated production adapters can read Alpaca and append/read Google
Sheets attestations; they contain no broker order methods and can never submit,
cancel, or replace a trade.

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

## Production adapters (disabled by default)

The Alpaca adapter has no general-purpose request method. Its complete allowlist is
`GET https://paper-api.alpaca.markets/v2/clock`, `GET /v2/calendar`, and these
`data.alpaca.markets` reads: `/v2/stocks/{symbol}/quotes/latest`,
`/v1beta1/options/snapshots/{underlying}`, and
`/v1beta1/options/quotes/latest`. Symbols are restricted to alphanumerics and a
dot. Bodies, redirects, other hosts/methods/paths, and anything containing order,
account, cancel, or replace are rejected. Raw bytes and parsed JSON are retained
with request ID, named feed, provider/receipt times, SHA-256, and an evidence seal.

The Google adapter allows only Values `GET` and the single Values `POST :append`
operation (Google uses POST for append). It has no update, clear, batch-update, or
delete surface. The append uses `RAW` and `INSERT_ROWS`, requires Google's response
to echo exactly one identical row, and the attestation boundary then GETs the exact
returned range and compares every cell before creating a trusted receipt.
Google's documented omission of trailing empty cells is normalized by padding only
the missing tail to the fixed seven-column schema; internal omissions, extra cells,
and other differences still fail. The exact header row is explicitly excluded from
attestation records.
The endpoint shapes follow the official [Alpaca Trading/Data API documentation](https://docs.alpaca.markets/reference)
and [Google Sheets Values API documentation](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values).
Service-account metadata must name exactly
`https://oauth2.googleapis.com/token`; OAuth uses the same no-redirect transport
policy and rejects every other URL or redirected response.

`SheetAttestationBoundary` serializes Google Sheets-compatible rows and permits
only append and read operations. It idempotently compares duplicate record IDs,
verifies exact read-back content before issuing an external receipt, reconciles
expected history, and represents fixes as new correction rows pointing at the old
row. It has no update or delete operation. No Google SDK or real spreadsheet ID is
used by this repository or CI.

An immutable attested envelope retains a deep snapshot of the complete original
artifact, its local ID and seal, the exact external read-back, a content-addressed
and sealed receipt, explicit parent IDs, and its own content-addressed ID and seal.
Verification recursively checks artifact-specific ancestry instead of trusting
caller-supplied attestation or launch Booleans. Launch eligibility is a verification
result, not an envelope field, and requires every ancestor plus receipt provenance
authenticated by an explicitly configured trusted-attestor policy. The disconnected
fixture policy is never trusted. Attestation never changes `PAPER ONLY` or
`NO LIVE ORDER`.

The Alpaca port exposes only clock, calendar, underlying quote, option-chain, and
option-quote reads. Captured evidence retains the untouched response, provider and
receipt timestamps, request ID, feed identity, and raw-response hash. CI uses the
injected fixture adapter and performs no network access.
Only timestamps actually present in the documented clock, latest-quote, or snapshot
response structures are recorded. Calendar and malformed/missing timestamp fields
remain explicitly unavailable, and timestamp-dependent use quarantines them rather
than substituting local receipt time.

### Future manual configuration

Install the optional runtime dependency with `pip install '.[live]'`. Operators
must inject (never write to a file, command line, log, or sheet)
`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `GOOGLE_SHEETS_SPREADSHEET_ID`, and
`GOOGLE_SERVICE_ACCOUNT_JSON` as process environment variables. The service
account must have access only to the intended spreadsheet; the Alpaca key should
be a dedicated least-privilege paper/read-only key. Credentials must
never be command-line arguments, logs, artifacts, repository files, or spreadsheet
values. The safe configuration check displays names and booleans only:

```bash
value-options preflight
```

It returns non-zero when any value is absent and always remains launch-ineligible.
Configuration alone does not authorize connection, ledger access, or fund launch.

After independent review, manually enable network reads in that one process only:

```bash
export VALUE_OPTIONS_ENABLE_LIVE_READ_ONLY=I_UNDERSTAND_READ_ONLY_NETWORK_ACCESS
value-options preflight --live-read-only
unset VALUE_OPTIONS_ENABLE_LIVE_READ_ONLY
```

This performs only an Alpaca clock GET and a Google values GET. It reports zero
writes and zero orders. It never appends an attestation, accesses account state,
or calls a broker write endpoint. Do not run this command in CI. Attestation
appends must be invoked by a separately reviewed operator workflow, followed by
the adapter's exact-range read-back before any receipt is trusted.

Exchange sessions are never inferred from weekdays. Live and replay processing
require a verified, sealed `MarketCalendarEvidence` observation whose coverage
contains the event and a subsequent open session. The read-only evidence port is
suitable for persisting an Alpaca calendar response. Missing, future-dated,
out-of-coverage, unverified, or seal-invalid evidence quarantines the event.

## Deliberate limitations

* Live adapters are inert until credentials and the exact activation sentinel are
  supplied; CI never supplies either and uses fixture transports only.
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

## Paper workflow orchestration (disabled by default)

The operator workflow has four deliberately independent authority boundaries:

1. **Read-only network activation** uses
   `VALUE_OPTIONS_ENABLE_LIVE_READ_ONLY=I_UNDERSTAND_READ_ONLY_NETWORK_ACCESS`.
   It permits only the allowlisted evidence GETs and never permits Sheet appends.
2. **Paper-ledger append activation** separately uses
   `VALUE_OPTIONS_ENABLE_PAPER_LEDGER_APPEND=I_AUTHORIZE_APPEND_ONLY_PAPER_LEDGER`.
   Before an append, `workflow-preflight` verifies spreadsheet identity, the
   `Attestations` tab, and its exact seven-column header. Every actual append is
   followed by an exact-range GET and immutable envelope; rows are never updated
   or deleted.
3. **Paper-fund launch authorization** remains a separate verified launch
   workflow. Neither sentinel, an artifact, rehearsal, nor receipt changes it.
4. **Live brokerage is prohibited.** There is no submit, cancel, replace, or live
   order capability. `PAPER ONLY` and `NO LIVE ORDER` remain immutable.

Production connections and appends are off unless their respective exact sentinel
is present. CI supplies fixtures only, without network, credentials, or Sheet
writes. `workflow-rehearsal` is excluded and proves that cash, NAV, orders,
positions, and launch status retain the same fingerprint.

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
value-options workflow-preflight
value-options research-collect research-input.json --at 2026-08-07T12:33:00Z --output artifacts/research.json
value-options decision-collect artifacts/research.json decision-evidence.json --at 2026-08-07T13:42:00Z --submitted-at 2026-08-07T13:42:01Z --decision-output artifacts/decision.json --submission-output artifacts/submission.json
value-options fill-collect artifacts/research.json artifacts/decision.json artifacts/submission.json post-submission-option-quote.json --as-of 2026-08-07T13:42:05Z --output artifacts/fill.json
value-options workflow-rehearsal evidence-bundle.json --as-of 2026-08-07T13:40:30Z --output artifacts/rehearsal.json
value-options inspect tests/fixtures/alpaca_opra_quote.json --as-of 2026-08-07T13:40:03Z --output artifacts/inspection.json
```

The collect commands enforce research → decision → submission → fill ancestry.
Research accepts only underlying/thesis pairs and clock, calendar, and underlying
quote references. Decision uses fresh complete evidence, OPRA contract data, and
the mandate/risk engine, then seals decision separately before simulated
submission. Fill re-verifies all ancestors and requires a new exact-contract OPRA
quote observed and received after submission; buys use ask and sells use bid.

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
