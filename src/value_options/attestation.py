"""External, append/read-back attestation without any network implementation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from typing import Any, Mapping, Sequence

from .market_data import canonical_json
from .models import require_utc


@dataclass(frozen=True)
class AttestationReceipt:
    artifact_id: str
    external_system: str
    appended_at: datetime
    immutable_location: str
    read_back_at: datetime
    content_sha256: str

    def __post_init__(self) -> None:
        require_utc(self.appended_at, "appended_at")
        require_utc(self.read_back_at, "read_back_at")
        if not all((self.artifact_id, self.external_system, self.immutable_location)):
            raise ValueError("complete external receipt identity is required")
        if self.read_back_at < self.appended_at:
            raise ValueError("read-back cannot predate append")
        if len(self.content_sha256) != 64:
            raise ValueError("a SHA-256 content hash is required")

    def as_json(self) -> dict[str, str]:
        value = asdict(self)
        value["appended_at"] = self.appended_at.isoformat()
        value["read_back_at"] = self.read_back_at.isoformat()
        return value


def content_hash(artifact: Mapping[str, Any]) -> str:
    """Hash the exact bytes represented by the artifact at append time."""
    return hashlib.sha256(canonical_json(artifact)).hexdigest()


def attest_artifact(artifact: Mapping[str, Any], receipt: AttestationReceipt,
                    read_back_content: Mapping[str, Any], *,
                    parents: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Mark a paper artifact attested only after exact, hashed read-back equivalence."""
    if receipt.artifact_id != artifact.get("artifact_id"):
        raise ValueError("receipt artifact mismatch")
    expected = content_hash(artifact)
    if receipt.content_sha256 != expected:
        raise ValueError("receipt content hash mismatch")
    if canonical_json(read_back_content) != canonical_json(artifact):
        raise ValueError("read-back content differs from appended artifact")
    if any(not p.get("externally_attested") for p in parents):
        raise ValueError("entire parent chain must be externally attested")
    result = dict(artifact)
    result.update(externally_attested=True,
                  launch_eligible=all(p.get("externally_attested") and
                                      p.get("launch_eligible", True) for p in parents),
                  attestation_receipt=receipt.as_json(),
                  classification="PAPER ONLY", order_policy="NO LIVE ORDER")
    return result
