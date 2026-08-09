"""Environment-only configuration discovery and safe diagnostics."""
from __future__ import annotations
import os
import re
from typing import Mapping

REQUIRED_ENV = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY",
                "GOOGLE_SHEETS_SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")
LIVE_ENABLE_ENV = "VALUE_OPTIONS_ENABLE_LIVE_READ_ONLY"
LEDGER_ENABLE_ENV = "VALUE_OPTIONS_ENABLE_PAPER_LEDGER_APPEND"
_SECRET_NAME = re.compile(r"(secret|token|password|credential|api.?key|service_account)", re.I)


def configuration_status(environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    values = os.environ if environ is None else environ
    return {name: bool(values.get(name)) for name in REQUIRED_ENV}


def redact(value, *, key="", environ: Mapping[str, str] | None = None):
    """Recursively redact credential-shaped fields; safe for logs/artifacts."""
    if _SECRET_NAME.search(key): return "[REDACTED]"
    values = os.environ if environ is None else environ
    secrets = sorted({values.get(name, "") for name in REQUIRED_ENV if values.get(name)},
                     key=len, reverse=True)
    if isinstance(value, str):
        for secret in secrets: value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, BaseException): return redact(str(value), environ=values)
    if isinstance(value, Mapping):
        return {k: redact(v, key=str(k), environ=values) for k, v in value.items()}
    if isinstance(value, list): return [redact(v, environ=values) for v in value]
    if isinstance(value, tuple): return tuple(redact(v, environ=values) for v in value)
    return value


def preflight(environ: Mapping[str, str] | None = None) -> tuple[bool, dict]:
    status = configuration_status(environ)
    present = all(status.values())
    return present, {"configuration": status, "launch_eligible": False,
                     "provider_checks_activated": False, "sheet_schema_verified": False,
                     "classification": "PAPER ONLY", "order_policy": "NO LIVE ORDER"}


def live_read_only_enabled(environ: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environ is None else environ
    return values.get(LIVE_ENABLE_ENV) == "I_UNDERSTAND_READ_ONLY_NETWORK_ACCESS"


def paper_ledger_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """A separate capability switch; network-read activation never implies writes."""
    values = os.environ if environ is None else environ
    return values.get(LEDGER_ENABLE_ENV) == "I_AUTHORIZE_APPEND_ONLY_PAPER_LEDGER"
