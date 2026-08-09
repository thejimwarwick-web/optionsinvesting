import json
import re
from datetime import datetime, timedelta, timezone

from value_options.cli import main
from value_options.operations import seal_artifact
from value_options.sheets import GoogleSheetsAdapter, HEADERS, SheetAttestationBoundary


UTC = timezone.utc
AT = datetime(2026, 8, 7, 12, 33, tzinfo=UTC)


class MemorySheet:
    def __init__(self):
        self.rows = []
        self.reads = 0

    def read_all(self):
        return tuple(self.rows)

    def append_row(self, row):
        self.rows.append(tuple(row))
        return f"Attestations!A{len(self.rows)+1}:G{len(self.rows)+1}"

    def read_row(self, location):
        self.reads += 1
        return self.rows[int(re.search(r"!A(\d+):", location).group(1))-2]


def test_sheet_preflight_is_exact_and_read_only():
    adapter = object.__new__(GoogleSheetsAdapter)
    adapter._spreadsheet, adapter._range = "expected", "Attestations!A:G"
    adapter._request = lambda *a, **k: {"values": [list(HEADERS)]}
    result = adapter.preflight("expected")
    assert result["verified"] and result["writes"] == 0 and result["header"] == list(HEADERS)


def test_ledger_append_needs_separate_sentinel_and_reads_back_exactly():
    port = MemorySheet(); boundary = SheetAttestationBoundary(port)
    artifact = seal_artifact("research", {"research_id": "fixture"})
    try:
        boundary.append_activated(artifact, AT, environ={})
    except ValueError as error:
        assert "disabled" in str(error)
    else:
        raise AssertionError("append occurred without activation")
    assert not port.rows
    result = boundary.append_activated(artifact, AT, environ={
        "VALUE_OPTIONS_ENABLE_PAPER_LEDGER_APPEND": "I_AUTHORIZE_APPEND_ONLY_PAPER_LEDGER"})
    assert result[3] is True and result[2][0].verify() and port.reads == 1 and len(port.rows) == 1


def test_rehearsal_command_reports_no_fund_mutation(tmp_path):
    bundle = tmp_path / "bundle.json"; output = tmp_path / "report.json"
    bundle.write_text('{"packets": []}')
    assert main(["workflow-rehearsal", str(bundle), "--as-of",
                 "2026-08-07T13:40:30+00:00", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["excluded"] and report["fund_state_unchanged"]
    assert report["classification"] == "PAPER ONLY" and report["order_policy"] == "NO LIVE ORDER"


def test_research_collection_rejects_option_evidence_before_sealing(tmp_path):
    source = tmp_path / "input.json"; output = tmp_path / "output.json"
    source.write_text(json.dumps({"candidates": [{"underlying": "AAPL", "thesis": "value"}],
        "evidence_references": [{"name": "option_chain", "value": "forbidden",
            "available_at": (AT-timedelta(seconds=1)).isoformat()}]}))
    assert main(["research-collect", str(source), "--at", AT.isoformat(),
                 "--output", str(output)]) == 1
    assert json.loads(output.read_text())["quarantined"]
