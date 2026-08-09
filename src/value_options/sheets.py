"""Google Sheets-compatible, append-only boundary (no Google client included)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from urllib.parse import quote, urlencode, urlsplit
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .attestation import AttestationReceipt
from .market_data import canonical_json
from .models import require_utc
from .http import Transport, UrllibTransport

HEADERS = ("record_id", "record_type", "artifact_id", "content_sha256", "appended_at",
           "payload_json", "corrects_record_id")


@dataclass(frozen=True)
class SheetRecord:
    record_id: str
    record_type: str
    artifact_id: str
    content_sha256: str
    appended_at: datetime
    payload_json: str
    corrects_record_id: str = ""

    def row(self) -> list[str]:
        return [self.record_id, self.record_type, self.artifact_id, self.content_sha256,
                self.appended_at.isoformat(), self.payload_json, self.corrects_record_id]

    @classmethod
    def create(cls, artifact: Mapping[str, Any], at: datetime, *, record_type="artifact",
               corrects_record_id="") -> "SheetRecord":
        require_utc(at, "appended_at")
        payload = canonical_json(artifact).decode()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        identity = hashlib.sha256(canonical_json({"artifact_id": artifact.get("artifact_id"),
            "content_sha256": digest, "record_type": record_type,
            "corrects_record_id": corrects_record_id})).hexdigest()
        return cls(identity, record_type, str(artifact.get("artifact_id", "")), digest, at,
                   payload, corrects_record_id)

    @classmethod
    def from_row(cls, row: Sequence[str]) -> "SheetRecord":
        if len(row) != len(HEADERS): raise ValueError("invalid sheet row width")
        return cls(row[0], row[1], row[2], row[3], datetime.fromisoformat(row[4]), row[5], row[6])

    def verify(self) -> bool:
        try: payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError): return False
        expected = SheetRecord.create(payload, self.appended_at, record_type=self.record_type,
                                      corrects_record_id=self.corrects_record_id)
        return (self.record_id, self.artifact_id, self.content_sha256) == \
               (expected.record_id, expected.artifact_id, expected.content_sha256)


class AppendOnlySheetPort(Protocol):
    """Implementations may only append rows and read values."""
    def append_row(self, row: Sequence[str]) -> str: ...
    def read_row(self, immutable_location: str) -> Sequence[str]: ...
    def read_all(self) -> Iterable[Sequence[str]]: ...


class GoogleSheetsAdapter:
    """Narrow Google Values adapter: GET values and POST append only.

    OAuth token acquisition is delegated to an injected token provider (normally
    ``google.auth``); tokens and spreadsheet identity are accepted only from the
    process environment. There is intentionally no update, batchUpdate or clear.
    """
    HOST = "sheets.googleapis.com"

    def __init__(self, *, token_provider, transport: Transport | None = None,
                 environ: Mapping[str, str] | None = None, sheet_range="Attestations!A:G"):
        env = os.environ if environ is None else environ
        self._spreadsheet = env.get("GOOGLE_SHEETS_SPREADSHEET_ID", "")
        if not self._spreadsheet or not self._spreadsheet.replace("-", "").replace("_", "").isalnum():
            raise ValueError("valid Google spreadsheet ID environment value required")
        if not env.get("GOOGLE_SERVICE_ACCOUNT_JSON"): raise ValueError("Google service-account environment credential required")
        self._token_provider, self._transport, self._range = token_provider, transport or UrllibTransport(), sheet_range

    def _request(self, method, suffix, *, body=None, query=None, value_range=None):
        if method not in {"GET", "POST"}: raise ValueError("Google method not allowlisted")
        base = f"/v4/spreadsheets/{self._spreadsheet}/values/"
        path = base + quote(self._range if value_range is None else value_range, safe="") + suffix
        if (method, suffix) not in {("GET", ""), ("POST", ":append")}:
            raise ValueError("Google endpoint not allowlisted")
        url = f"https://{self.HOST}{path}" + ("?" + urlencode(query) if query else "")
        payload = None if body is None else canonical_json(body)
        if method == "GET" and payload is not None: raise ValueError("GET request bodies forbidden")
        response = self._transport.request(method, url, headers={"Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json"}, body=payload)
        final = urlsplit(response.url)
        if final.scheme != "https" or final.hostname != self.HOST or 300 <= response.status < 400:
            raise ValueError("Google redirect or unapproved response host")
        if response.status < 200 or response.status >= 300: raise ValueError(f"Google Sheets failed with HTTP {response.status}")
        return json.loads(response.body or b"{}")

    def append_row(self, row):
        result = self._request("POST", ":append", body={"majorDimension": "ROWS", "values": [list(row)]},
            query={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS", "includeValuesInResponse": "true"})
        updates = result.get("updates", {})
        if updates.get("updatedRows") != 1 or updates.get("updatedData", {}).get("values") != [list(row)]:
            raise ValueError("Google append response did not exactly echo one row")
        location = updates.get("updatedRange", "")
        if not location: raise ValueError("Google append omitted updated range")
        return location

    def read_row(self, immutable_location):
        rows = self._request("GET", "", value_range=immutable_location).get("values", [])
        if len(rows) != 1: raise ValueError("expected exactly one Google row")
        return rows[0]

    def read_all(self): return tuple(self._request("GET", "").get("values", ()))


def google_service_account_token_provider(environ: Mapping[str, str] | None = None):
    """Build a refresh-on-use provider using Google's official auth library."""
    env = os.environ if environ is None else environ
    info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials
    credentials = Credentials.from_service_account_info(info,
        scopes=("https://www.googleapis.com/auth/spreadsheets",))
    def token():
        if not credentials.valid: credentials.refresh(Request())
        return credentials.token
    return token


class FixtureAttestorPolicy:
    """Disconnected fixtures can verify content but can never authorize launch."""
    def trusts(self, receipt: AttestationReceipt) -> bool: return False


class SheetAttestationBoundary:
    def __init__(self, port: AppendOnlySheetPort, external_system="google-sheets"):
        self.port, self.external_system = port, external_system

    def append(self, artifact: Mapping[str, Any], at: datetime) -> tuple[SheetRecord, str, bool]:
        record = SheetRecord.create(artifact, at)
        for row in self.port.read_all():
            existing = SheetRecord.from_row(row)
            if existing.record_id == record.record_id:
                if existing.row() != record.row(): raise ValueError("duplicate record ID has different content")
                return existing, f"record:{existing.record_id}", False
        return record, self.port.append_row(record.row()), True

    def read_back(self, record: SheetRecord, location: str, at: datetime) -> tuple[AttestationReceipt, Mapping[str, Any]]:
        require_utc(at, "read_back_at")
        actual = SheetRecord.from_row(self.port.read_row(location))
        if actual.row() != record.row() or not actual.verify():
            raise ValueError("sheet read-back does not exactly match append")
        payload = json.loads(actual.payload_json)
        return AttestationReceipt.create(artifact_id=record.artifact_id,
            external_system=self.external_system, appended_at=record.appended_at,
            immutable_location=location, read_back_at=at,
            content_sha256=record.content_sha256, provenance="disconnected-fixture"), payload

    def correction(self, bad_record_id: str, corrected_artifact: Mapping[str, Any], at: datetime) -> tuple[SheetRecord, str]:
        if not any(SheetRecord.from_row(row).record_id == bad_record_id for row in self.port.read_all()):
            raise ValueError("correction target does not exist")
        record = SheetRecord.create(corrected_artifact, at, record_type="correction",
                                    corrects_record_id=bad_record_id)
        return record, self.port.append_row(record.row())

    def reconcile(self, expected: Iterable[SheetRecord]) -> tuple[str, ...]:
        expected_by_id = {x.record_id: x for x in expected}
        actual = [SheetRecord.from_row(x) for x in self.port.read_all()]
        actual_by_id = {}
        duplicates = []
        for row in actual:
            if row.record_id in actual_by_id: duplicates.append(f"{row.record_id}: duplicate external record ID")
            else: actual_by_id[row.record_id] = row
        differences = []
        for key in sorted(expected_by_id.keys() | actual_by_id.keys()):
            if key not in actual_by_id: differences.append(f"{key}: missing")
            elif key not in expected_by_id: differences.append(f"{key}: unexpected")
            elif actual_by_id[key].row() != expected_by_id[key].row(): differences.append(f"{key}: mismatch")
        return tuple(duplicates + differences)
