from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest

from value_options.attestation import (AttestationReceipt, AttestedArtifact,
    create_attested_artifact, verify_attested_artifact)
from value_options.broker import AlpacaReadOnlyClient, FixtureAlpaca, ProviderResponse, ReadOnlyAlpaca
from value_options.http import HttpResponse
from value_options.sheets import GoogleSheetsAdapter
from value_options.cli import main
from value_options.config import preflight, redact
from value_options.operations import seal_artifact
from value_options.sheets import FixtureAttestorPolicy, SheetAttestationBoundary

UTC = timezone.utc
AT = datetime(2026, 8, 7, 13, 42, tzinfo=UTC)


class MemorySheet:
    def __init__(self): self.rows = []
    def append_row(self, row):
        self.rows.append(list(row)); return f"sheet://ledger/rows/{len(self.rows)}"
    def read_row(self, location): return self.rows[int(location.rsplit("/", 1)[1]) - 1]
    def read_all(self): return tuple(tuple(row) for row in self.rows)


class TrustedForTest:
    def trusts(self, receipt): return receipt.provenance == "configured-production-adapter"


def envelope(artifact, parents=(), *, trusted=False):
    port = MemorySheet(); boundary = SheetAttestationBoundary(port)
    row, location, _ = boundary.append(artifact, AT)
    receipt, readback = boundary.read_back(row, location, AT)
    if trusted:
        receipt = AttestationReceipt.create(artifact_id=receipt.artifact_id,
            external_system=receipt.external_system, appended_at=receipt.appended_at,
            immutable_location=receipt.immutable_location, read_back_at=receipt.read_back_at,
            content_sha256=receipt.content_sha256, provenance="configured-production-adapter")
    return create_attested_artifact(artifact, receipt, readback, parents=parents,
        trusted_attestor=TrustedForTest() if trusted else FixtureAttestorPolicy())


def chain(*, trusted=False):
    research = seal_artifact("research", {"research_id": "research-record"})
    ar = envelope(research, trusted=trusted)
    decision = seal_artifact("decision", {"research_id": "research-record"})
    ad = envelope(decision, [ar], trusted=trusted)
    submission = seal_artifact("submission", {"decision_artifact_id": decision["artifact_id"]})
    ass = envelope(submission, [ad], trusted=trusted)
    fill = seal_artifact("fill", {"submission_artifact_id": submission["artifact_id"], "price": "1"})
    return ar, ad, ass, envelope(fill, [ass], trusted=trusted)


def test_recursive_attested_envelope_preserves_original_and_fixture_is_ineligible():
    ar, ad, ass, af = chain()
    result = verify_attested_artifact(af, trusted_attestor=FixtureAttestorPolicy())
    assert result.verified and not result.launch_eligible and not hasattr(af, "launch_eligible")
    assert af.original_artifact["classification"] == "PAPER ONLY"
    assert af.original_artifact["order_policy"] == "NO LIVE ORDER"
    assert af.parent_artifact_ids == (ass.local_artifact_id,)


def test_only_configured_trusted_provenance_can_be_launch_eligible():
    *_, fill = chain(trusted=True)
    assert verify_attested_artifact(fill, trusted_attestor=TrustedForTest()).launch_eligible
    # Correct hashes from a caller-created fixture receipt do not establish provenance.
    checked = verify_attested_artifact(fill, trusted_attestor=FixtureAttestorPolicy())
    assert checked.verified and not checked.launch_eligible


def test_fabricated_boolean_wrong_substituted_missing_and_extra_parents_rejected():
    research = seal_artifact("research", {"research_id": "r1"}); ar = envelope(research)
    decision = seal_artifact("decision", {"research_id": "r1"})
    with pytest.raises((ValueError, AttributeError)):
        envelope(decision, [{"externally_attested": True, "launch_eligible": True}])
    with pytest.raises((ValueError, AttributeError)):
        envelope(decision, [{"artifact_id": ar.local_artifact_id}])
    wrong = envelope(seal_artifact("research", {"research_id": "other"}))
    with pytest.raises(ValueError, match="exact single"):
        envelope(decision, [wrong])
    with pytest.raises(ValueError, match="exact single"):
        envelope(decision, [ar, wrong])
    fill = seal_artifact("fill", {"submission_artifact_id": "missing"})
    with pytest.raises(ValueError, match="exact single"):
        envelope(fill)
    # Real immutable envelopes contain no eligibility Boolean at all.
    assert not hasattr(ar, "launch_eligible")


def test_cycle_and_post_attestation_modification_are_detected():
    ar, _, _, af = chain()
    object.__setattr__(ar, "parents", (ar,))
    assert not verify_attested_artifact(ar).verified
    damaged = replace(af, artifact_json=af.artifact_json.replace(b'"price":"1"', b'"price":"9"'))
    checked = verify_attested_artifact(damaged)
    assert not checked.verified and any("local artifact" in x or "content hash" in x for x in checked.reasons)


def test_tampered_duplicate_reconciliation_and_append_only_correction():
    artifact = seal_artifact("research", {"research_id": "r"})
    port = MemorySheet(); boundary = SheetAttestationBoundary(port)
    record, location, appended = boundary.append(artifact, AT)
    assert appended and not boundary.append(artifact, AT)[2]
    port.rows.append(record.row())
    assert any("duplicate external record ID" in x for x in boundary.reconcile([record]))
    port.rows.pop(); port.rows[0][5] = port.rows[0][5].replace('"r"', '"x"')
    with pytest.raises(ValueError, match="read-back"): boundary.read_back(record, location, AT)
    port.rows[0] = record.row()
    correction, _ = boundary.correction(record.record_id, artifact, AT)
    assert correction.record_type == "correction" and len(port.rows) == 2


def test_read_only_alpaca_surface_preserves_evidence_and_rejects_writes():
    raw = {"quote": {"bp": 1, "ap": 2}}
    response = ProviderResponse.capture("option_quote", "request-1", "OPRA", AT, AT, raw)
    client = FixtureAlpaca({"option_quote": response})
    assert client.option_quote("IBM") == response and response.raw is raw
    assert response.raw_sha256 == hashlib.sha256(b'{"quote":{"ap":2,"bp":1}}').hexdigest()
    assert not hasattr(ReadOnlyAlpaca, "submit_order")
    with pytest.raises(AttributeError): client.submit_order({})
    with pytest.raises(ValueError, match="write-side"): FixtureAlpaca({"cancel_order": response})


def test_environment_preflight_and_secrets_inside_innocent_strings_and_errors(capsys):
    secrets = {"ALPACA_API_KEY_ID": "key-123", "ALPACA_API_SECRET_KEY": "secret-456",
        "GOOGLE_SHEETS_SPREADSHEET_ID": "sheet-789", "GOOGLE_SERVICE_ACCOUNT_JSON": "json-abc"}
    ok, report = preflight({})
    assert not ok and not report["launch_eligible"] and not any(report["configuration"].values())
    message = {"ordinary": "failed using key-123 and secret-456", "error": ValueError("sheet-789 json-abc")}
    cleaned = redact(message, environ=secrets)
    assert all(secret not in str(cleaned) for secret in secrets.values())
    assert main(["preflight"]) == 2 and "secret-456" not in capsys.readouterr().out
    with pytest.raises(SystemExit): main(["preflight", "--api-key", "secret-456"])


class FakeHttp:
    def __init__(self, response): self.response=response; self.calls=[]
    def request(self, method, url, *, headers, body=None):
        self.calls.append((method,url,headers,body)); return self.response


def test_production_alpaca_is_exact_get_only_and_preserves_wire_bytes():
    wire=b'{"timestamp":"2026-08-07T13:42:00Z","is_open":true}'
    transport=FakeHttp(HttpResponse(200,{"x-request-id":"req-42"},wire,
        "https://paper-api.alpaca.markets/v2/clock"))
    client=AlpacaReadOnlyClient(transport=transport,environ={
        "ALPACA_API_KEY_ID":"not-real", "ALPACA_API_SECRET_KEY":"not-real"})
    result=client.clock()
    assert transport.calls[0][0]=="GET" and transport.calls[0][3] is None
    assert result.raw_response==wire and result.request_id=="req-42" and result.evidence_seal
    with pytest.raises(ValueError,match="invalid symbol"): client.underlying_quote("AAPL/orders")
    redirected=FakeHttp(HttpResponse(302,{"Location":"https://evil.test"},b"",
        "https://evil.test/steal"))
    with pytest.raises(ValueError,match="redirect"):
        AlpacaReadOnlyClient(transport=redirected,environ={
            "ALPACA_API_KEY_ID":"x","ALPACA_API_SECRET_KEY":"y"}).clock()


def test_live_preflight_remains_disabled_without_activation(monkeypatch, capsys):
    for name in ("ALPACA_API_KEY_ID","ALPACA_API_SECRET_KEY","GOOGLE_SHEETS_SPREADSHEET_ID","GOOGLE_SERVICE_ACCOUNT_JSON"):
        monkeypatch.setenv(name,"fixture-only")
    assert main(["preflight","--live-read-only"])==2
    assert "disabled" not in capsys.readouterr().out  # diagnostics expose counts, not secrets/errors


def test_google_adapter_append_allowlist_and_exact_echo():
    row=["id","artifact","a","hash",AT.isoformat(),"{}",""]
    response=(b'{"updates":{"updatedRows":1,"updatedRange":"Attestations!A2:G2",'
              b'"updatedData":{"values":[["id","artifact","a","hash",'
              b'"2026-08-07T13:42:00+00:00","{}",""]]}}}')
    transport=FakeHttp(HttpResponse(200,{},response,
        "https://sheets.googleapis.com/v4/spreadsheets/sheet_fixture/values/Attestations%21A%3AG:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS&includeValuesInResponse=true"))
    adapter=GoogleSheetsAdapter(token_provider=lambda:"token",transport=transport,environ={
        "GOOGLE_SHEETS_SPREADSHEET_ID":"sheet_fixture","GOOGLE_SERVICE_ACCOUNT_JSON":"{}"})
    assert adapter.append_row(row)=="Attestations!A2:G2"
    method,url,headers,body=transport.calls[0]
    assert method=="POST" and ":append?" in url and b'"values"' in body
    assert "token" not in body.decode()
