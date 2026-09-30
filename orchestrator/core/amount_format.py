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
