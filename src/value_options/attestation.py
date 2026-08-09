"""Immutable external-attestation envelopes and recursive verification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from .market_data import canonical_json
from .models import require_utc
from .operations import verify_artifact


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class AttestationReceipt:
    artifact_id: str
    external_system: str
    appended_at: datetime
    immutable_location: str
    read_back_at: datetime
    content_sha256: str
    provenance: str
    receipt_id: str
    seal: str

    @classmethod
    def create(cls, *, artifact_id: str, external_system: str, appended_at: datetime,
               immutable_location: str, read_back_at: datetime, content_sha256: str,
               provenance: str = "untrusted-caller") -> "AttestationReceipt":
        body = {"artifact_id": artifact_id, "external_system": external_system,
                "appended_at": appended_at.isoformat(), "immutable_location": immutable_location,
                "read_back_at": read_back_at.isoformat(), "content_sha256": content_sha256,
                "provenance": provenance}
        receipt_id = _sha(body)
        return cls(artifact_id, external_system, appended_at, immutable_location, read_back_at,
                   content_sha256, provenance, receipt_id,
                   _sha({"receipt_id": receipt_id, **body}))

    def body(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "external_system": self.external_system,
                "appended_at": self.appended_at.isoformat(),
                "immutable_location": self.immutable_location,
                "read_back_at": self.read_back_at.isoformat(),
                "content_sha256": self.content_sha256, "provenance": self.provenance}

    def as_json(self) -> dict[str, str]:
        return {"receipt_id": self.receipt_id, **self.body(), "seal": self.seal}

    def verify(self) -> bool:
        try:
            require_utc(self.appended_at, "appended_at"); require_utc(self.read_back_at, "read_back_at")
        except ValueError:
            return False
        return (bool(self.artifact_id and self.external_system and self.immutable_location and self.provenance)
                and self.read_back_at >= self.appended_at
                and bool(re.fullmatch(r"[0-9a-f]{64}", self.content_sha256))
                and self.receipt_id == _sha(self.body())
                and self.seal == _sha({"receipt_id": self.receipt_id, **self.body()}))


class TrustedAttestor(Protocol):
    """Future production adapters must authenticate their own receipt provenance."""
    def trusts(self, receipt: AttestationReceipt) -> bool: ...


@dataclass(frozen=True)
class AttestedArtifact:
    """A deep-immutable envelope; artifacts are stored as their canonical bytes."""
    artifact_json: bytes
    local_artifact_id: str
    local_artifact_seal: str
    receipt: AttestationReceipt
    read_back_json: bytes
    external_system: str
    immutable_location: str
    parent_artifact_ids: tuple[str, ...]
    parents: tuple["AttestedArtifact", ...]
    envelope_id: str
    seal: str

    @property
    def original_artifact(self) -> dict[str, Any]:
        return json.loads(self.artifact_json)

    @property
    def exact_read_back(self) -> dict[str, Any]:
        return json.loads(self.read_back_json)

    def body(self) -> dict[str, Any]:
        return {"original_artifact": self.original_artifact,
                "local_artifact_id": self.local_artifact_id,
                "local_artifact_seal": self.local_artifact_seal,
                "receipt": self.receipt.as_json(), "exact_read_back": self.exact_read_back,
                "external_system": self.external_system,
                "immutable_location": self.immutable_location,
                "parent_artifact_ids": list(self.parent_artifact_ids)}

    def as_json(self) -> dict[str, Any]:
        return {"envelope_id": self.envelope_id, **self.body(),
                "parents": [parent.as_json() for parent in self.parents], "seal": self.seal}


def load_attested_artifact(value: Mapping[str, Any]) -> AttestedArtifact:
    """Load a supplied parentless envelope verbatim; verification is separate."""
    r=value["receipt"]
    receipt=AttestationReceipt(r["artifact_id"],r["external_system"],datetime.fromisoformat(r["appended_at"]),
        r["immutable_location"],datetime.fromisoformat(r["read_back_at"]),r["content_sha256"],
        r["provenance"],r["receipt_id"],r["seal"])
    parents=tuple(load_attested_artifact(parent) for parent in value.get("parents",()))
    return AttestedArtifact(canonical_json(value["original_artifact"]),value["local_artifact_id"],
        value["local_artifact_seal"],receipt,canonical_json(value["exact_read_back"]),
        value["external_system"],value["immutable_location"],tuple(value.get("parent_artifact_ids",())),
        parents,value["envelope_id"],value["seal"])


@dataclass(frozen=True)
class AttestationVerification:
    verified: bool
    launch_eligible: bool
    reasons: tuple[str, ...]


def content_hash(artifact: Mapping[str, Any]) -> str:
    return _sha(artifact)


def _expected_parents(artifact: Mapping[str, Any], parents: Sequence[AttestedArtifact]) -> tuple[str, ...]:
    kind, payload = artifact.get("artifact_kind"), artifact.get("payload", {})
    if any(not isinstance(parent, AttestedArtifact) for parent in parents):
        raise ValueError("parents must be verified attested-artifact envelopes")
    if kind in {"research", "evidence", "portfolio_snapshot"}:
        if parents: raise ValueError(f"{kind} cannot have parents")
        return ()
    if kind == "decision":
        matches = [p for p in parents if p.original_artifact.get("artifact_kind") == "research"
                   and p.original_artifact.get("payload", {}).get("research_id") == payload.get("research_id")]
    elif kind == "submission":
        matches = [p for p in parents if p.local_artifact_id == payload.get("decision_artifact_id")
                   and p.original_artifact.get("artifact_kind") == "decision"]
    elif kind == "fill":
        matches = [p for p in parents if p.local_artifact_id == payload.get("submission_artifact_id")
                   and p.original_artifact.get("artifact_kind") == "submission"]
    else: raise ValueError(f"unsupported attested artifact kind: {kind}")
    if len(parents) != 1 or len(matches) != 1:
        raise ValueError(f"{kind} requires its exact single attested parent")
    return (matches[0].local_artifact_id,)


def create_attested_artifact(artifact: Mapping[str, Any], receipt: AttestationReceipt,
                             read_back_content: Mapping[str, Any], *,
                             parents: Sequence[AttestedArtifact] = (),
                             trusted_attestor: TrustedAttestor | None = None) -> AttestedArtifact:
    """Create a new envelope without modifying or sharing mutable artifact state."""
    artifact_bytes, read_bytes = canonical_json(artifact), canonical_json(read_back_content)
    parent_ids = _expected_parents(artifact, parents)
    body = {"original_artifact": json.loads(artifact_bytes),
            "local_artifact_id": artifact.get("artifact_id"),
            "local_artifact_seal": artifact.get("seal"), "receipt": receipt.as_json(),
            "exact_read_back": json.loads(read_bytes), "external_system": receipt.external_system,
            "immutable_location": receipt.immutable_location,
            "parent_artifact_ids": list(parent_ids)}
    envelope_id = _sha(body); seal = _sha({"envelope_id": envelope_id, **body})
    candidate = AttestedArtifact(artifact_bytes, str(artifact.get("artifact_id", "")),
        str(artifact.get("seal", "")), receipt, read_bytes, receipt.external_system,
        receipt.immutable_location, parent_ids, tuple(parents), envelope_id, seal)
    result = verify_attested_artifact(candidate, trusted_attestor=trusted_attestor)
    if not result.verified: raise ValueError("; ".join(result.reasons))
    return candidate


def verify_attested_artifact(envelope: AttestedArtifact, *,
                             trusted_attestor: TrustedAttestor | None = None,
                             _seen: frozenset[str] = frozenset()) -> AttestationVerification:
    reasons: list[str] = []
    if envelope.envelope_id in _seen: return AttestationVerification(False, False, ("cyclic ancestry",))
    artifact = envelope.original_artifact
    kind = str(artifact.get("artifact_kind", ""))
    local_ok, local_reasons = verify_artifact(artifact, kind)
    if not local_ok: reasons.extend(f"local artifact: {x}" for x in local_reasons)
    if envelope.local_artifact_id != artifact.get("artifact_id"): reasons.append("local artifact ID mismatch")
    if envelope.local_artifact_seal != artifact.get("seal"): reasons.append("local artifact seal mismatch")
    if not envelope.receipt.verify(): reasons.append("receipt seal or fields invalid")
    if envelope.receipt.artifact_id != envelope.local_artifact_id: reasons.append("receipt artifact mismatch")
    if envelope.receipt.content_sha256 != _sha(artifact): reasons.append("receipt content hash mismatch")
    if envelope.read_back_json != envelope.artifact_json: reasons.append("external read-back differs")
    if envelope.external_system != envelope.receipt.external_system: reasons.append("external system mismatch")
    if envelope.immutable_location != envelope.receipt.immutable_location: reasons.append("immutable location mismatch")
    try: expected = _expected_parents(artifact, envelope.parents)
    except ValueError as error: reasons.append(str(error)); expected = ()
    if envelope.parent_artifact_ids != expected: reasons.append("explicit parent IDs mismatch")
    body = envelope.body()
    if envelope.envelope_id != _sha(body): reasons.append("attested envelope ID mismatch")
    if envelope.seal != _sha({"envelope_id": envelope.envelope_id, **body}): reasons.append("attested envelope seal mismatch")
    parent_eligible = True
    for parent in envelope.parents:
        checked = verify_attested_artifact(parent, trusted_attestor=trusted_attestor,
                                           _seen=_seen | {envelope.envelope_id})
        reasons.extend(f"parent {parent.local_artifact_id}: {x}" for x in checked.reasons)
        parent_eligible &= checked.verified and checked.launch_eligible
    trusted = bool(trusted_attestor and trusted_attestor.trusts(envelope.receipt))
    verified = not reasons
    # Eligibility exists only in this freshly computed result, never in the envelope.
    return AttestationVerification(verified, verified and trusted and parent_eligible, tuple(reasons))


# Compatibility name: it now returns an immutable envelope, never a modified artifact.
attest_artifact = create_attested_artifact
