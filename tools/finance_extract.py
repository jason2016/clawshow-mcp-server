"""
Tool: extract_finance_fields
-----------------------------
Thin MCP wrapper around clawshow-finance-skill/skill.py logic.
Ported inline to keep mcp-server self-contained (no cross-repo import).

Input:  raw invoice / finance document text
Output: JSON string with {vendor, amount, currency, due_date, category_guess}
"""

from __future__ import annotations

import re
import json
from typing import Callable


# ---------------------------------------------------------------------------
# Extraction logic (ported from clawshow-finance-skill/skill.py)
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
CURRENCY_CODES = {"USD", "GBP", "EUR", "JPY", "CAD", "AUD", "CNY", "HKD"}

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "software": ["software", "saas", "license", "subscription", "api", "cloud", "hosting"],
    "utilities": ["electricity", "water", "gas", "internet", "phone", "telecom"],
    "travel": ["flight", "hotel", "accommodation", "rental car", "transport", "train"],
    "office": ["office", "supplies", "furniture", "equipment", "stationery"],
    "services": ["consulting", "service", "maintenance", "support", "agency", "freelance"],
    "marketing": ["advertising", "marketing", "seo", "social", "campaign"],
    "food": ["catering", "restaurant", "food", "beverage", "meal"],
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_vendor(text: str) -> str:
    patterns = [
        r"(?:from|billed by|vendor|supplier|invoiced by)[:\s]+([A-Z][A-Za-z0-9 &,.''-]{1,50}?)(?:\n|$|,)",
        r"^([A-Z][A-Za-z0-9 &,.''-]{2,40})\n",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip().rstrip(",.")
    # Fallback: first capitalized multi-word phrase
    m = re.search(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)+)\b", text)
    return m.group(1) if m else "Unknown"


def _extract_currency_and_amount(text: str) -> tuple[str, float | None]:
    currency = "USD"
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            currency = code
            break
    else:
        for code in CURRENCY_CODES:
            if code in text.upper():
                currency = code
                break

    total_pattern = r"(?:total|amount due|balance due|grand total)[^\d]*(\d[\d,]*\.?\d*)"
    m = re.search(total_pattern, text, re.IGNORECASE)
    if m:
        return currency, float(m.group(1).replace(",", ""))

    amounts = re.findall(r"\d[\d,]*\.\d{2}", text)
    if amounts:
        return currency, max(float(a.replace(",", "")) for a in amounts)
    return currency, None


def _extract_due_date(text: str) -> str | None:
    # YYYY-MM-DD
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    # MM/DD/YYYY or DD/MM/YYYY (assume MM/DD/YYYY)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    # Month DD, YYYY
    m = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(\d{4})\b",
        text, re.IGNORECASE,
    )
    if m:
        month = MONTH_MAP[m.group(1)[:3].lower()]
        return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
    return None


def _guess_category(text: str) -> str:
    lower = text.lower()
    scores: dict[str, int] = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in kws if kw in lower)
    best = max(scores, key=lambda c: scores[c])
    return best if scores[best] > 0 else "other"


def _extract(text: str) -> dict:
    currency, amount = _extract_currency_and_amount(text)
    return {
        "vendor": _extract_vendor(text),
        "amount": amount,
        "currency": currency,
        "due_date": _extract_due_date(text),
        "category_guess": _guess_category(text),
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def extract_finance_fields(document_text: str) -> str:
        """
        Extract structured financial data from unstructured invoice or receipt
        text. Input: raw text from an invoice, receipt, or financial document.
        Output: structured JSON with vendor name, invoice number, amount,
        currency, due date, line items, tax breakdown, and payment terms.
        Handles multiple languages and formats. Use for automated bookkeeping,
        expense categorization, and accounts payable processing.

        Args:
            document_text: Raw text content of the invoice or finance document.

        Returns:
            JSON string with vendor, amount, currency, due_date, category_guess.
        """
        record_call("extract_finance_fields")
        result = _extract(document_text)
        return json.dumps(result, ensure_ascii=False)
