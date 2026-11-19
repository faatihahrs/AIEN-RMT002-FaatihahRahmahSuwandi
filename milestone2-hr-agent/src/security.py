"""Security helpers. Complete every TODO before persisting telemetry."""
from __future__ import annotations

import hashlib
import re


EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+62|62|0)8[\d\s().-]{7,14}\d(?!\d)"
)

TOKEN_PATTERN = re.compile(
    r"\b(?:token|bearer|api[_-]?key|secret)[\s:=_-]*[A-Za-z0-9._-]{6,}\b",
    re.IGNORECASE,
)

MEDICAL_FIELD_PATTERN = re.compile(
    r"\b(?:diagnosis|diagnosa|medical\s*condition|kondisi\s*medis)"
    r"\s*:\s*[^.;,\n]+",
    re.IGNORECASE,
)

MEDICAL_TERM_PATTERN = re.compile(
    r"\b(?:diabetes|hipertensi|kanker|tuberkulosis|hiv|aids)\b",
    re.IGNORECASE,
)


def token_hash(token: str) -> str:
    """Return a deterministic digest without storing the raw bearer token."""
    if not token:
        raise ValueError("token tidak boleh kosong")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def stable_safety_id(employee_id: str) -> str:
    """Return a stable, non-identifying provider safety identifier."""
    if not employee_id:
        raise ValueError("employee_id tidak boleh kosong")

    digest = hashlib.sha256(
        f"hr-assistant:{employee_id}".encode("utf-8")
    ).hexdigest()

    return f"employee-{digest[:32]}"

def provider_error_category(exc: Exception) -> str:
    """Convert provider exceptions into non-sensitive telemetry categories."""
    status_code = getattr(exc, "status_code", None)

    if status_code in {401, 403}:
        return "provider_authentication_failed"

    if status_code == 429:
        return "provider_rate_limited"

    if status_code in {408, 504}:
        return "provider_timeout"

    if isinstance(status_code, int) and status_code >= 500:
        return "provider_unavailable"

    error_type = type(exc).__name__.casefold()

    if "authentication" in error_type or "permission" in error_type:
        return "provider_authentication_failed"

    if "ratelimit" in error_type:
        return "provider_rate_limited"

    if "timeout" in error_type:
        return "provider_timeout"

    if "connection" in error_type:
        return "provider_connection_failed"

    return "provider_request_failed"

def redact(value: str) -> str:
    """Remove common PII, credentials, and medical details from telemetry."""
    if not value:
        return value

    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    redacted = TOKEN_PATTERN.sub("[REDACTED_TOKEN]", redacted)
    redacted = MEDICAL_FIELD_PATTERN.sub("[REDACTED_MEDICAL]", redacted)
    redacted = MEDICAL_TERM_PATTERN.sub("[REDACTED_MEDICAL]", redacted)

    return redacted

def redact_other_employee_ids(
    text: str,
    authenticated_employee_id: str,
) -> str:
    """Remove employee IDs that do not belong to the active identity."""
    employee_id_pattern = re.compile(
        r"\bEMP-\d+\b",
        flags=re.IGNORECASE,
    )
    own_id = authenticated_employee_id.upper()

    return employee_id_pattern.sub(
        lambda match: (
            match.group(0)
            if match.group(0).upper() == own_id
            else "[REDACTED_EMPLOYEE_ID]"
        ),
        text,
    )