from datetime import datetime, timezone
import hashlib

import pytest

from value_options.attestation import attest_artifact
from value_options.broker import FixtureAlpaca, ProviderResponse, ReadOnlyAlpaca
from value_options.cli import main
from value_options.config import preflight, redact
from value_options.operations import seal_artifact
from value_options.sheets import SheetAttestationBoundary, SheetRecord

UTC = timezone.utc
AT = datetime(2026, 8, 7, 13, 42, tzinfo=UTC)


class MemorySheet:
    def __init__(self): self.rows = []
    def append_row(self, row):
        self.rows.append(list(row)); return f"sheet://ledger/rows/{len(self.rows)}"
    def read_row(self, location): return self.rows[int(location.rsplit("/", 1)[1]) - 1]
    def read_all(self): return tuple(tuple(row) for row in self.rows)


def test_append_readback_attests_chain_and_never_changes_paper_policy():
    port = MemorySheet(); boundary = SheetAttestationBoundary(port)
    research = seal_artifact("research", {"result": "IBM"})
    row, location, appended = boundary.append(research, AT)
    receipt, readback = boundary.read_back(row, location, AT)
    attested_research = attest_artifact(research, receipt, readback)
    assert appended and attested_research["launch_eligible"]

    fill = seal_artifact("fill", {"price": "1.00"})
    fill_row, fill_location, _ = boundary.append(fill, AT)
    fill_receipt, fill_readback = boundary.read_back(fill_row, fill_location, AT)
    attested_fill = attest_artifact(fill, fill_receipt, fill_readback,
                                    parents=[attested_research])
    assert attested_fill["externally_attested"] and attested_fill["launch_eligible"]
    assert (attested_fill["classification"], attested_fill["order_policy"]) == \
           ("PAPER ONLY", "NO LIVE ORDER")


def test_tampering_hash_duplicates_parent_chain_and_corrections_fail_closed():
    port = MemorySheet(); boundary = SheetAttestationBoundary(port)
    artifact = seal_artifact("fill", {"price": "1.00"})
    record, location, _ = boundary.append(artifact, AT)
    _, _, appended = boundary.append(artifact, AT)
    assert not appended and len(port.rows) == 1
    port.rows[0][5] = port.rows[0][5].replace("1.00", "9.00")
    with pytest.raises(ValueError, match="read-back"):
        boundary.read_back(record, location, AT)
    port.rows[0] = record.row()
    receipt, readback = boundary.read_back(record, location, AT)
    bad = receipt.__class__(receipt.artifact_id, receipt.external_system, receipt.appended_at,
                            receipt.immutable_location, receipt.read_back_at, "0" * 64)
    with pytest.raises(ValueError, match="hash"):
        attest_artifact(artifact, bad, readback)
    with pytest.raises(ValueError, match="parent chain"):
        attest_artifact(artifact, receipt, readback,
                        parents=[seal_artifact("submission", {})])
    correction, _ = boundary.correction(record.record_id, artifact, AT)
    assert correction.record_type == "correction" and len(port.rows) == 2


def test_read_only_alpaca_surface_preserves_evidence_and_rejects_writes():
    raw = {"quote": {"bp": 1, "ap": 2}}
    response = ProviderResponse.capture("option_quote", "request-1", "OPRA", AT, AT, raw)
    client = FixtureAlpaca({"option_quote": response})
    assert client.option_quote("IBM") == response
    assert response.raw is raw
    assert response.raw_sha256 == hashlib.sha256(b'{"quote":{"ap":2,"bp":1}}').hexdigest()
    assert not hasattr(ReadOnlyAlpaca, "submit_order")
    with pytest.raises(AttributeError): client.submit_order({})
    with pytest.raises(ValueError, match="write-side"):
        FixtureAlpaca({"cancel_order": response})


def test_environment_only_preflight_and_secret_redaction(capsys):
    ok, report = preflight({})
    assert not ok and not report["launch_eligible"] and not any(report["configuration"].values())
    assert main(["preflight"]) == 2
    assert "secret-value" not in capsys.readouterr().out
    assert redact({"api_key": "secret-value", "nested": {"password": "hunter2"}}) == \
           {"api_key": "[REDACTED]", "nested": {"password": "[REDACTED]"}}
    with pytest.raises(SystemExit): main(["preflight", "--api-key", "secret-value"])

