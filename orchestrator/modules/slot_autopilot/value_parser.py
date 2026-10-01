import re
from decimal import Decimal, InvalidOperation

from orchestrator.core.amount_format import grouped_amount_match, normalize_number_run, strip_thousands_spaces
from orchestrator.core.result import Result

_CURRENCY_TOKENS = re.compile(r"(€|\$|EUR|USD)", re.IGNORECASE)
_NUMERIC_RUN = re.compile(r"[\dOoIlSsB.,\-]*\d[\dOoIlSsB.,\-]*")
_DIGIT_CLASS = r"\dOoIlSsB"
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
    grouped = grouped_amount_match(cleaned, _DIGIT_CLASS)
    if grouped is not None and re.search(r"\d", grouped.group(1)) and (
        match is None or grouped.start(1) <= match.start() + 1
    ):
        # Migliaia separate da uno spazio ('49 999,90'): un unico importo. Un '-' subito prima e' il segno.
        run = strip_thousands_spaces(grouped.group(1))
        if grouped.start(1) > 0 and cleaned[grouped.start(1) - 1] == "-":
            run = "-" + run
    elif match:
        run = match.group(0)
    else:
        return Result.fail(f"No numeric content found in: {text!r}", "AMOUNT_NOT_FOUND")
    negative = run.strip().startswith("-")
    run = run.translate(_OCR_DIGIT_FIXES)
    run = run.lstrip("-")

    normalized = normalize_number_run(run)

    if negative:
        normalized = f"-{normalized}"

    try:
        return Result.ok(Decimal(normalized))
    except InvalidOperation as exc:
        return Result.fail(f"Cannot parse amount from {text!r}: {exc}", "AMOUNT_PARSE_ERROR")
