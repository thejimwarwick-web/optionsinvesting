import json
from pathlib import Path

from value_options.accounting import AppendOnlyLedgerSink
from value_options.broker import ReadOnlyAlpaca
from value_options.calendar import ReadOnlyCalendarEvidenceSource


ROOT = Path(__file__).parent


def test_mandate_conformance_matrix_is_machine_readable_and_complete():
    matrix = json.loads((ROOT / "mandate_conformance.json").read_text())
    assert matrix["schema_version"] == 1
    rules = matrix["rules"]
    assert len(rules) == 36
    assert len({rule["rule_id"] for rule in rules}) == len(rules)
    available_tests = {
        line.split("(", 1)[0].removeprefix("def ")
        for path in ROOT.glob("test_*.py")
        for line in path.read_text().splitlines()
        if line.startswith("def test_")
    }
    for rule in rules:
        assert rule["passing"], rule["rule_id"]
        assert rule["rejection_or_boundary"], rule["rule_id"]
        assert set(rule["passing"] + rule["rejection_or_boundary"]) <= available_tests


def test_read_only_boundaries_have_no_mutating_methods():
    for boundary in (ReadOnlyAlpaca, ReadOnlyCalendarEvidenceSource, AppendOnlyLedgerSink):
        methods = {name.lower() for name in vars(boundary) if not name.startswith("_")}
        assert not methods & {"submit_order", "cancel_order", "replace_order", "update", "delete"}
