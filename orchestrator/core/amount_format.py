import re
from typing import Optional

# Spazio, spazio non separabile e spazio sottile: separatori delle migliaia ("49 999.90", "6 405 921.88").
_THOUSANDS_SPACES = " \u00a0\u202f"


def grouped_amount_match(text: str, digit: str = r"\d"):
    """Match (re.Match) del primo importo con le migliaia separate da uno spazio, o None. Lo spazio vale
    come separatore SOLO tra un gruppo di 1-3 cifre e gruppi di ESATTAMENTE 3 cifre, dopo un eventuale
    decimale di 1-2 cifre: '49 999.90', '1 250,00'. Non unisce '0.10 0.20', '10 25', '1234 567'.
    `digit` e' il contenuto di una classe di caratteri (per accettare anche le cifre confuse dall'OCR)."""
    d = f"[{digit}]"
    pattern = (
        rf"(?<![{digit}.,])({d}{{1,3}}(?:[{_THOUSANDS_SPACES}]{d}{{3}})+(?:[.,]{d}{{1,2}})?)(?!{d})"
    )
    return re.search(pattern, text)


def strip_thousands_spaces(run: str) -> str:
    return re.sub(rf"[{_THOUSANDS_SPACES}]", "", run)


def find_amount_run(text: str, allow_single_digit: bool = False) -> Optional[str]:
    """Il primo importo del testo, gia' normalizzato ('49999.90'), o None. Accetta le migliaia separate da
    spazio e la sequenza con punti/virgole di normalize_number_run; vince quello che inizia prima."""
    plain = re.search(r"\d[\d.,]*\d|\d" if allow_single_digit else r"\d[\d.,]*\d", text)
    grouped = grouped_amount_match(text)
    if grouped is not None and (plain is None or grouped.start(1) <= plain.start()):
        return normalize_number_run(strip_thousands_spaces(grouped.group(1)))
    if plain is None:
        return None
    return normalize_number_run(plain.group(0))


def normalize_number_run(run: str) -> str:
    """Una sequenza di cifre con punti/virgole -> stringa leggibile da Decimal/float
    ('1234.56'). Regole, pensate per gli importi di un casino (due decimali):
    - due tipi di separatore: vince l'ultimo come decimale ('1.234,56', '5,000.00');
    - un solo tipo, ripetuto: migliaia ('1.000.000'); se l'ultimo gruppo non ha 3 cifre
      e' il decimale ('12.345.67');
    - un solo separatore seguito da ESATTAMENTE 3 cifre, dopo 1-3 cifre non tutte zero
      ('50.000', '1,250'): migliaia. Tre decimali non esistono negli importi;
    - altrimenti decimale ('0.10', '114,36', '0.125')."""
    last_comma, last_dot = run.rfind(","), run.rfind(".")
    if last_comma >= 0 and last_dot >= 0:
        if last_comma > last_dot:
            return run.replace(".", "").replace(",", ".")
        return run.replace(",", "")
    separator = "," if last_comma >= 0 else ("." if last_dot >= 0 else None)
    if separator is None:
        return run
    parts = run.split(separator)
    if len(parts) > 2:
        if len(parts[-1]) == 3:
            return "".join(parts)
        return "".join(parts[:-1]) + "." + parts[-1]
    integer, fraction = parts
    if len(fraction) == 3 and 1 <= len(integer) <= 3 and integer.strip("0") != "":
        return integer + fraction
    return integer + "." + fraction
