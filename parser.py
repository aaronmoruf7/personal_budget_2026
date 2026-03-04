from __future__ import annotations

import re
from datetime import date

# Matches lines like "3/2" or "12/31"
DATE_LINE = re.compile(r"^\d{1,2}/\d{1,2}$")

# Matches lines like "$10.90" or "+$825.00"
AMOUNT_LINE = re.compile(r"^\+?\$[\d,]+\.\d{2}$")


def _clean_name(parts: list[str]) -> str:
    """Remove 'logo' noise, deduplicate, return first unique name."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for part in parts:
        # Remove trailing " logo" (case-insensitive)
        part = re.sub(r"\s+logo\s*$", "", part, flags=re.IGNORECASE).strip()
        key = part.lower()
        if part and key not in seen:
            seen.add(key)
            cleaned.append(part)
    return cleaned[0] if cleaned else "Unknown"


def parse_transactions(raw_text: str, year: int | None = None) -> list[dict]:
    """
    Parse copy-pasted Rocket Money transaction text into a list of dicts.

    Expected paste structure per transaction (with blank lines between columns):
        <date line>           e.g. "3/2"
        <name line(s)>        may repeat or include "logo"
        <category line>       e.g. "Shopping"
        <amount line>         e.g. "$10.90" or "+$825.00"

    Returns list of dicts: {date, name, category, amount, is_income}
    """
    if year is None:
        year = date.today().year

    lines = [line.strip() for line in raw_text.splitlines()]

    # Group lines into per-transaction chunks, split on date lines
    groups: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if DATE_LINE.match(line):
            if current:
                groups.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        groups.append(current)

    results: list[dict] = []
    for group in groups:
        # Drop blank lines
        non_blank = [l for l in group if l.strip()]

        # Need at least: date, category, amount
        if len(non_blank) < 3:
            continue

        date_str = non_blank[0]
        amount_str = non_blank[-1]

        if not AMOUNT_LINE.match(amount_str):
            continue

        category = non_blank[-2]
        name_parts = non_blank[1:-2]

        is_income = amount_str.startswith("+")
        amount = float(
            amount_str.replace("+", "").replace("$", "").replace(",", "")
        )

        try:
            m, d = map(int, date_str.split("/"))
            txn_date = date(year, m, d)
        except (ValueError, TypeError):
            continue

        results.append(
            {
                "date": txn_date,
                "name": _clean_name(name_parts),
                "category": category,
                "amount": amount,
                "is_income": is_income,
            }
        )

    return results
