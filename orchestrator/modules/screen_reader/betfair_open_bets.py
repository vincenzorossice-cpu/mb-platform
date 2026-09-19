import re
from dataclasses import dataclass
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.screen_reader.reader import ScreenReader

# mesi italiani abbreviati, duplicato localmente (non importato da movement_importer:
# screen_reader e' un modulo di livello piu' basso, non deve dipendere da esso)
_ITALIAN_MONTHS = {
    "gen": "01", "feb": "02", "mar": "03", "apr": "04", "mag": "05", "giu": "06",
    "lug": "07", "ago": "08", "set": "09", "ott": "10", "nov": "11", "dic": "12",
}

# formato Betfair: "19-set-26 13:40:45" (trattino, anno a 2 cifre, con orario) -
# diverso dal formato PayPal "13 set 2026" (spazio, anno a 4 cifre, senza orario)
_BETFAIR_DATETIME_RE = re.compile(r"^(\d{1,2})-([a-zA-Zà-ù]{3})-(\d{2})\s+(\d{2}:\d{2}:\d{2})$")
_BET_ID_RE = re.compile(r"ID scommessa Betfair (\d+:\d+)")
_ABBINATE_RE = re.compile(r"Abbinate:\s*(\d{1,2}-[a-zA-Zà-ù]{3}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_BARE_VALUE_RE = re.compile(r"^[-+]?[\d.,]+$")

# regex euro/italiana duplicata localmente (non importata da reader.py: _AMOUNT_RE e'
# una costante privata di modulo, non un metodo semi-pubblico come _clean_italian_amount)
_LOCAL_AMOUNT_RE = re.compile(
    r"[-+]?\(?\s*€?\s*"
    r"(?:\d{1,3}(?:\.\d{3})+,\d{2}"
    r"|\d+,\d{2}"
    r"|\d+\.\d{2})"
    r"\s*€?\)?"
)


@dataclass
class BetfairOpenBet:
    """Bancata Betfair aperta, letta dalla schermata Scommesse Exchange -> Aperte."""
    bet_id: str              # "1:443519021791", da "ID scommessa Betfair ..."
    event: str                # "Gil Vicente - Maritimo"
    selection: str             # "Pareggio"
    bet_type: str              # "Tipo" -> "Banca"
    lay_odds: float            # "Quota" -> quota lay piazzata su Betfair
    lay_stake: float           # "Puntata (€)" -> importo lay ("Puntata" sul lato Betfair e' la LAY stake)
    liability: float           # "Responsabilita' (€)" -> il vero "open_bets" da incrociare con impegnato_netcap
    potential_profit: float    # "Profitto potenziale (€)"
    status: str                # "Stato" -> "Abbinate" | altro
    market: Optional[str] = None       # "Esito Finale"
    placed_at: Optional[str] = None    # ISO 8601, da "Piazzata 19-set-26 13:40:45"
    matched_at: Optional[str] = None   # ISO 8601, da "...| Abbinate: 19-set-26 15:39:37"


class BetfairOpenBetsReader:
    """Legge le bancate aperte dalla schermata Betfair 'Scommesse Exchange -> Aperte'."""

    SCREEN_TYPE = "open_bets"

    def __init__(self, screen_reader: ScreenReader):
        self.screen_reader = screen_reader

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def read_open_bets(self, device_id: str) -> Result[list[BetfairOpenBet]]:
        """Legge le scommesse Exchange aperte da Betfair via ScreenReader.

        Il device deve essere gia' sulla schermata 'Scommesse Exchange -> Aperte':
        non esiste (ancora) una destinazione TouchInjector per navigarci."""
        screen = self.screen_reader.read_screen(device_id, screen_type=self.SCREEN_TYPE, use_cache=False)
        if not screen.success:
            return Result.fail(f"Lettura schermata Aperte fallita: {screen.error}", screen.error_code)

        texts = [e.text for e in screen.data.elements]
        return self._parse_open_bets(texts)

    # ------------------------------------------------------------------
    # private — parsing
    # ------------------------------------------------------------------

    def _empty_pending(self) -> dict:
        return {"placed_at": None, "bet_id": None, "matched_at": None,
                 "event": None, "selection": None, "market": None}

    def _parse_open_bets(self, texts: list[str]) -> Result[list[BetfairOpenBet]]:
        """Singolo passaggio sugli elementi: accumula i campi 'pending' che
        precedono l'ancora di blocco ('Tipo'/'Banca'), poi delega a
        _parse_bet_block per i campi tra l'ancora di inizio e quella di fine
        ('Stato'/valore)."""
        bets: list[BetfairOpenBet] = []
        pending = self._empty_pending()

        i = 0
        n = len(texts)
        while i < n:
            t = texts[i].strip()

            if t.startswith("Piazzata "):
                pending["placed_at"] = self._parse_betfair_datetime(t[len("Piazzata "):].strip())
                i += 1
                continue

            if t.startswith("Dettagli"):
                bet_id_match = _BET_ID_RE.search(t)
                if bet_id_match:
                    pending["bet_id"] = bet_id_match.group(1)
                matched_match = _ABBINATE_RE.search(t)
                if matched_match:
                    pending["matched_at"] = self._parse_betfair_datetime(matched_match.group(1))
                i += 1
                continue

            if pending["event"] is None and re.match(r"^.+ - .+$", t):
                pending["event"] = t
                pending["selection"] = texts[i + 1].strip() if i + 1 < n else None
                pending["market"] = texts[i + 2].strip() if i + 2 < n else None
                i += 3
                continue

            if t == "Tipo" and i + 1 < n and texts[i + 1].strip() == "Banca":
                bet, next_i = self._parse_bet_block(texts, i + 2, pending)
                if bet is not None:
                    bets.append(bet)
                pending = self._empty_pending()
                i = next_i
                continue

            i += 1

        return Result.ok(bets)

    def _parse_bet_block(self, texts: list[str], start: int, pending: dict) -> tuple[Optional["BetfairOpenBet"], int]:
        """Scansiona da 'start' fino all'ancora di fine ('Stato'/valore),
        estraendo Quota/Puntata/Responsabilita'/Profitto potenziale. Ritorna
        (None, indice) se manca un campo obbligatorio (bancata malformata)."""
        n = len(texts)
        lay_odds = lay_stake = liability = potential_profit = None
        status = "unknown"
        j = start

        while j < n:
            t = texts[j].strip()
            t_lower = t.lower()

            if t == "Stato":
                status = texts[j + 1].strip() if j + 1 < n else "unknown"
                j += 2
                break

            if t_lower.startswith("quota"):
                lay_odds, j = self._extract_labeled_value(texts, j)
            elif t_lower.startswith("puntata"):
                lay_stake, j = self._extract_labeled_value(texts, j)
            elif t_lower.startswith("responsabilit"):
                liability, j = self._extract_labeled_value(texts, j)
            elif t_lower.startswith("profitto potenziale"):
                potential_profit, j = self._extract_labeled_value(texts, j)
            else:
                j += 1

        if lay_odds is None or lay_stake is None or liability is None:
            return None, j

        bet = BetfairOpenBet(
            bet_id=pending["bet_id"] or "",
            event=pending["event"] or "",
            selection=pending["selection"] or "",
            bet_type="Banca",
            lay_odds=lay_odds,
            lay_stake=lay_stake,
            liability=liability,
            potential_profit=potential_profit or 0.0,
            status=status,
            market=pending["market"],
            placed_at=pending["placed_at"],
            matched_at=pending["matched_at"],
        )
        return bet, j

    def _extract_labeled_value(self, texts: list[str], index: int) -> tuple[Optional[float], int]:
        """Preferisce il valore 'bare' che segue la riga etichetta (es.
        'Puntata (€) 461,66' seguita da '461,66'), per evitare di riparsare la
        riga etichetta e ottenere un doppio match; altrimenti ricade sul
        parsing della riga etichetta stessa."""
        label_line = texts[index]
        next_line = texts[index + 1] if index + 1 < len(texts) else ""
        if _BARE_VALUE_RE.match(next_line.strip()):
            return self.screen_reader._clean_italian_amount(next_line.strip()), index + 2

        match = _LOCAL_AMOUNT_RE.search(label_line)
        value = self.screen_reader._clean_italian_amount(match.group(0)) if match else None
        return value, index + 1

    def _parse_betfair_datetime(self, text: str) -> Optional[str]:
        match = _BETFAIR_DATETIME_RE.match(text.strip())
        if not match:
            return None
        day, mon_abbr, yy, time_str = match.groups()
        month = _ITALIAN_MONTHS.get(mon_abbr.lower())
        if month is None:
            return None
        return f"20{yy}-{month}-{int(day):02d}T{time_str}"
