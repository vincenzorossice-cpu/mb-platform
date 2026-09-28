import re
from decimal import Decimal, InvalidOperation

from orchestrator.core.result import Result

_CURRENCY_TOKENS = re.compile(r"(€|\$|EUR|USD)", re.IGNORECASE)
_NUMERIC_RUN = re.compile(r"[\dOoIlSsB.,\-]*\d[\dOoIlSsB.,\-]*")
_OCR_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8"})


def parse_amount(text: str) -> Result[Decimal]:
    """Estrae un importo da un testo OCR rumoroso: rimuove i simboli di valuta,
    isola la sequenza numerica, corregge le confusioni OCR piu' comuni
    (O/o->0, l/I->1, S->5, B->8) SOLO dentro quella sequenza, poi applica la
    stessa regola "ultimo separatore vince" di SlotOCRReader._extract_amount."""
    if not text:
        return Result.fail("Empty text", "AMOUNT_NOT_FOUND")

    cleaned = _CURRENCY_TOKENS.sub("", text)
    match = _NUMERIC_RUN.search(cleaned)
    if not match:
        return Result.fail(f"No numeric content found in: {text!r}", "AMOUNT_NOT_FOUND")

    run = match.group(0)
    negative = run.strip().startswith("-")
    run = run.translate(_OCR_DIGIT_FIXES)
    run = run.lstrip("-")

    last_comma = run.rfind(",")
    last_dot = run.rfind(".")
    if last_comma > last_dot:
        normalized = run.replace(".", "").replace(",", ".")
    elif last_dot > last_comma:
        normalized = run.replace(",", "")
    elif "," in run:
        normalized = run.replace(",", ".")
    else:
        normalized = run

    if negative:
        normalized = f"-{normalized}"

    try:
        return Result.ok(Decimal(normalized))
    except InvalidOperation as exc:
        return Result.fail(f"Cannot parse amount from {text!r}: {exc}", "AMOUNT_PARSE_ERROR")
