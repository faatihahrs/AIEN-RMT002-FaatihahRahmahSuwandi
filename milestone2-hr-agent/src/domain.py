"""Deterministic leave-domain rules; this module must never call the model."""
from __future__ import annotations

from datetime import date, timedelta

from .contracts import LeaveBalance, PrepareLeaveArgs

from datetime import date, timedelta
import re

class DomainError(ValueError):
    pass

INDONESIAN_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}

INDONESIAN_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})\s+"
    r"(januari|februari|maret|april|mei|juni|juli|agustus|"
    r"september|oktober|november|desember)\s+"
    r"(\d{4})\b",
    re.IGNORECASE,
)

ISO_DATE_PATTERN = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b"
)

LEAVE_ACTION_PATTERN = re.compile(
    r"\b(?:siapkan|ajukan|buatkan|pengajuan|proposal|"
    r"ingin\s+cuti|mau\s+cuti)\b",
    re.IGNORECASE,
)


def _explicit_dates(message: str) -> list[date]:
    """Extract explicit dates while preserving their order in the message."""
    matches: list[tuple[int, date]] = []

    for match in INDONESIAN_DATE_PATTERN.finditer(message):
        day_text, month_text, year_text = match.groups()

        try:
            parsed = date(
                int(year_text),
                INDONESIAN_MONTHS[month_text.lower()],
                int(day_text),
            )
        except ValueError:
            continue

        matches.append((match.start(), parsed))

    for match in ISO_DATE_PATTERN.finditer(message):
        year_text, month_text, day_text = match.groups()

        try:
            parsed = date(
                int(year_text),
                int(month_text),
                int(day_text),
            )
        except ValueError:
            continue

        matches.append((match.start(), parsed))

    matches.sort(key=lambda item: item[0])
    return [parsed for _, parsed in matches]

def has_reversed_explicit_leave_dates(message: str) -> bool:
    """Detect a reversed leave range before the model can rewrite it."""
    if not LEAVE_ACTION_PATTERN.search(message):
        return False

    dates = _explicit_dates(message)

    return len(dates) >= 2 and dates[1] < dates[0]

def business_days(start: date, end: date) -> int:
    """Count weekdays inclusively between start and end."""
    if end < start:
        raise DomainError("tanggal selesai tidak boleh sebelum tanggal mulai")

    count = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)

    if count == 0:
        raise DomainError(
            "rentang cuti harus memiliki minimal satu hari kerja"
        )

    return count


def validate_leave_request(
    args: PrepareLeaveArgs,
    balances: list[LeaveBalance],
    overlaps: bool,
    today: date,
) -> tuple[int, list[str]]:
    """Validate a proposed leave request without creating a side effect."""
    if args.end_date < args.start_date:
        raise DomainError(
            "tanggal selesai tidak boleh sebelum tanggal mulai"
        )

    if args.start_date < today:
        raise DomainError(
            "tanggal mulai cuti tidak boleh berada di masa lalu"
        )

    if overlaps:
        raise DomainError(
            "rentang cuti bertumpang tindih dengan pengajuan yang sudah ada"
        )

    requested_days = business_days(args.start_date, args.end_date)

    matching_balance = next(
        (
            balance
            for balance in balances
            if balance.leave_type == args.leave_type
        ),
        None,
    )

    if matching_balance is None:
        raise DomainError(
            f"saldo untuk tipe cuti {args.leave_type} tidak ditemukan"
        )

    if requested_days > matching_balance.available_days:
        raise DomainError(
            "saldo cuti tidak cukup untuk jumlah hari yang diminta"
        )

    validation_summary = [
        "Tanggal pengajuan valid.",
        f"Jumlah hari kerja: {requested_days}.",
        "Tidak bertumpang tindih dengan pengajuan lain.",
        (
            f"Saldo {args.leave_type} mencukupi: "
            f"{matching_balance.available_days} hari tersedia."
        ),
        (
            "Proposal belum membuat pengajuan cuti dan masih "
            "memerlukan konfirmasi pengguna."
        ),
    ]

    return requested_days, validation_summary


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    """Estimate cost using instructor-provided per-one-million-token rates."""
    if input_rate is None or output_rate is None:
        return None

    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("jumlah token tidak boleh negatif")

    input_cost = input_tokens / 1_000_000 * input_rate
    output_cost = output_tokens / 1_000_000 * output_rate

    return input_cost + output_cost