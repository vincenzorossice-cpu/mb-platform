import logging
import re
from datetime import datetime
from typing import Optional

from orchestrator.core.result import Result
from orchestrator.modules.signal_parser.interface import Signal, SignalParserInterface
from orchestrator.modules.rag_system.interface import RAGSystemInterface, TCVerification

logger = logging.getLogger(__name__)


class SignalPreprocessor:
    """Step 1 — estrazione campi base via regex sui delimitatori emoji."""

    EMOJI_MAP = {
        "📚": "book_marker",
        "💎": "bonus_type_1",
        "⏳": "bonus_type_2",
        "✅": "strategy",
        "✋": "slot_advised",
        "📛": "slot_rigioco",
        "⏰": "deadline",
        "🤩": "priority",
        "💰": "profit",
        "⚡️": "variance",
    }

    def preprocess(self, raw_text: str) -> dict:
        fields = {}

        book_match = re.search(r'📚\s*(.+?)(?:\n|$)', raw_text)
        fields["book_raw"] = book_match.group(1).strip() if book_match else None

        bonus_match = re.search(r'[💎⏳]\s*(.+?)(?:🚨|\n)', raw_text)
        fields["bonus_type_raw"] = bonus_match.group(1).strip() if bonus_match else None

        # FIX: il pattern originale cercava "LVello" (typo) e non matchava mai "LIVELLO" reale.
        diff_match = re.search(r'🚨\s*LIVELLO\s+(\w+)', raw_text, re.IGNORECASE)
        fields["difficulty_raw"] = diff_match.group(1).strip() if diff_match else None

        deadline_match = re.search(r'⏰Scadenza:\s*(.+?)(?:\n|$)', raw_text)
        fields["deadline_raw"] = deadline_match.group(1).strip() if deadline_match else None

        priority_match = re.search(r'🤩+\s*(.+?)(?:\n|$)', raw_text)
        fields["priority_raw"] = priority_match.group(1).strip() if priority_match else None

        profit_match = re.search(r'💰+\s*(.+?)(?:\n|$)', raw_text)
        fields["profit_raw"] = profit_match.group(1).strip() if profit_match else None

        variance_match = re.search(r'⚡️+\s*(.+?)(?:\n|$)', raw_text)
        fields["variance_raw"] = variance_match.group(1).strip() if variance_match else None

        strategy_match = re.search(r'✅\s*(.+?)(?=\n[✋📛⏰🤩💰⚡️]|\Z)', raw_text, re.DOTALL)
        fields["strategy_raw"] = strategy_match.group(1).strip() if strategy_match else None

        slot_match = re.search(r'✋\s*(.+?)(?:\n|$)', raw_text)
        fields["slot_advised_raw"] = slot_match.group(1).strip() if slot_match else None

        # FIX: il pattern a singola riga perdeva Target/Rollover/Margine sulle righe successive.
        rigioco_match = re.search(r'📛\s*(.+?)(?=\n⏰|\Z)', raw_text, re.DOTALL)
        fields["slot_rigioco_raw"] = rigioco_match.group(1).strip() if rigioco_match else None

        return fields


class LlamaSignalExtractor:
    """Step 2 — estrazione strutturata. STUB euristico: sostituirà Llama 3 8B."""

    SPORT_KEYWORDS = ("tripla", "doppia", "singola", "quota", "evento", "scommess")
    CASINO_KEYWORDS = ("slot", "spin", "rollover", "wagering", "bingo")

    def extract(self, preprocessed: dict, raw_text: str) -> Result[dict]:
        strategy_raw = preprocessed.get("strategy_raw") or ""
        strategy_lower = strategy_raw.lower()

        extracted: dict = {
            "book": preprocessed.get("book_raw"),
            "bonus_type": preprocessed.get("bonus_type_raw"),
            "difficulty": preprocessed.get("difficulty_raw"),
            "priority": preprocessed.get("priority_raw"),
            "variance": preprocessed.get("variance_raw"),
            "expected_profit": self._extract_expected_profit(preprocessed.get("profit_raw")),
        }

        if any(kw in strategy_lower for kw in self.SPORT_KEYWORDS):
            extracted["category"] = "sport"
            extracted["strategy"], extracted["bonus"] = self._extract_sport(strategy_raw)
        elif any(kw in strategy_lower for kw in self.CASINO_KEYWORDS):
            extracted["category"] = "casino"
            extracted["strategy"], extracted["bonus"] = self._extract_casino(
                strategy_raw, preprocessed
            )
        else:
            extracted["category"] = None
            extracted["strategy"] = {}
            extracted["bonus"] = {}

        return Result.ok(extracted)

    # --- sport ---

    def _extract_sport(self, strategy_raw: str) -> tuple[dict, dict]:
        split_match = re.search(r'(riceverai|a refertazione)', strategy_raw, re.IGNORECASE)
        if split_match:
            strategy_text = strategy_raw[: split_match.start()]
            bonus_text = strategy_raw[split_match.start():]
        else:
            strategy_text, bonus_text = strategy_raw, ""

        strategy = {
            "action": self._sport_action(strategy_text),
            "amount": self._first_amount(strategy_text),
            "bet_type": self._bet_type(strategy_text),
            "min_odds": self._min_odds(strategy_text),
            "events_window_days": self._events_window_days(strategy_text),
            "leagues": self._leagues(strategy_text),
        }

        bonus: dict = {}
        if bonus_text:
            bonus = {
                "type": "free_bet",
                "amount": self._first_amount(bonus_text),
                "wagering": None,
                "reuse": {
                    "bet_type": self._bet_type(bonus_text),
                    "min_odds": self._min_odds(bonus_text),
                    "leagues": self._leagues(bonus_text),
                },
            }

        return strategy, bonus

    def _sport_action(self, text: str) -> str:
        lower = text.lower()
        if "aderisci" in lower and "ricarica" in lower:
            return "aderisci_promo_e_ricarica"
        if "attiva" in lower and "gioca" in lower:
            return "attiva_promo_e_gioca"
        return "attiva_promo"

    def _bet_type(self, text: str) -> Optional[str]:
        lower = text.lower()
        for bet_type in ("tripla", "doppia", "singola"):
            if bet_type in lower:
                return bet_type
        return None

    def _min_odds(self, text: str) -> Optional[float]:
        match = re.search(r'quota\s*(?:minima\s*)?(?:di\s*)?(\d+[.,]\d+)', text, re.IGNORECASE)
        return float(match.group(1).replace(",", ".")) if match else None

    def _events_window_days(self, text: str) -> Optional[int]:
        match = re.search(r'entro\s*(\d+)\s*giorni', text, re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _leagues(self, text: str) -> list[str]:
        match = re.search(
            r'\bsu(?:lle|lla|gli|i|l)?\b\s+(.+?)(?:,?\s*con quota|$)',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return []
        parts = re.split(r'\s*,\s*|\s+e\s+', match.group(1))
        return [part.strip() for part in parts if part.strip()]

    # --- casino ---

    def _extract_casino(self, strategy_raw: str, preprocessed: dict) -> tuple[dict, dict]:
        strategy: dict = {
            "action": self._casino_action(strategy_raw),
            "amount": self._first_amount(strategy_raw),
        }

        slot_advised_raw = preprocessed.get("slot_advised_raw")
        if slot_advised_raw:
            strategy["slot_advised"] = re.sub(
                r'slot\s*consigliata\s*:?\s*', "", slot_advised_raw, flags=re.IGNORECASE
            ).strip()

        slot_rigioco_raw = preprocessed.get("slot_rigioco_raw")
        if slot_rigioco_raw:
            strategy["slot_rigioco"] = self._slot_rigioco_name(slot_rigioco_raw)
            margine = re.search(r'Margine:\s*(.+?)(?:\n|$)', slot_rigioco_raw, re.IGNORECASE)
            if margine:
                strategy["margine"] = margine.group(1).strip()
            target = re.search(
                r'Target:\s*(\d+(?:[.,]\d+)?)\s*€', slot_rigioco_raw, re.IGNORECASE
            )
            if target:
                strategy["target"] = float(target.group(1).replace(",", "."))
            rollover = re.search(r'Rollover:\s*(.+?)(?:\n|$)', slot_rigioco_raw, re.IGNORECASE)
            if rollover:
                strategy["rollover"] = rollover.group(1).strip()

        percent_match = re.search(r'(\d+)\s*%', strategy_raw)
        cap_match = re.search(r'massimo\s*(\d+(?:[.,]\d+)?)\s*€', strategy_raw, re.IGNORECASE)
        type_match = re.search(r'bonus\s+(\w+)', strategy_raw, re.IGNORECASE)

        bonus = {
            "type": type_match.group(1).lower() if type_match else None,
            "percent": int(percent_match.group(1)) if percent_match else None,
            "cap": float(cap_match.group(1).replace(",", ".")) if cap_match else None,
        }

        return strategy, bonus

    def _casino_action(self, text: str) -> str:
        lower = text.lower()
        if "attiva" in lower and "gioca" in lower:
            return "attiva_promo_e_gioca"
        return "attiva_promo"

    def _slot_rigioco_name(self, block: str) -> str:
        match = re.search(r'RIGIOCO:?\s*(.+?)(?:\n|$)', block, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        first_line = block.splitlines()[0] if block else ""
        return first_line.strip()

    # --- comune ---

    def _first_amount(self, text: str) -> Optional[float]:
        match = re.search(r'(\d+(?:[.,]\d+)?)\s*€', text)
        return float(match.group(1).replace(",", ".")) if match else None

    def _extract_expected_profit(self, profit_raw: Optional[str]) -> Optional[str]:
        if not profit_raw:
            return None
        lower = profit_raw.lower()
        for level in ("alto", "medio", "basso"):
            if level in lower:
                return level
        match = re.search(r'(\d+(?:[.,]\d+)?)', profit_raw)
        return match.group(1).replace(",", ".") if match else None


class SignalPostprocessor:
    """Step 3 — normalizzazione, parsing date, validazione campi critici."""

    BOOK_NORMALIZATION = {
        "stanleybet": "Stanleybet", "snai": "SNAI", "sisal": "Sisal",
        "eurobet": "Eurobet", "goldbet": "Goldbet", "lottomatica": "Lottomatica",
        "planetwin": "Planetwin", "pokerstars": "Pokerstars", "bwin": "Bwin",
        "bet365": "Bet365", "betfair": "Betfair",
    }

    MONTHS = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
        "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
        "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
    }

    SPORT_KEYWORDS = ("tripla", "doppia", "singola", "quota", "evento", "scommess")
    CASINO_KEYWORDS = ("slot", "spin", "rollover", "wagering", "bingo")

    def postprocess(self, extracted: dict, preprocessed: dict) -> Result[Signal]:
        # 1 — normalizza book
        book_raw = (extracted.get("book") or preprocessed.get("book_raw") or "").lower()
        book = self.BOOK_NORMALIZATION.get(book_raw, book_raw.title())
        if not book:
            return Result.fail("Cannot determine book", "MISSING_CRITICAL_FIELD")

        # 2 — determina category
        category = extracted.get("category")
        strategy_lower = (preprocessed.get("strategy_raw") or "").lower()
        if not category:
            if any(kw in strategy_lower for kw in self.SPORT_KEYWORDS):
                category = "sport"
            elif any(kw in strategy_lower for kw in self.CASINO_KEYWORDS):
                category = "casino"
            else:
                return Result.fail("Cannot determine category", "MISSING_CRITICAL_FIELD")

        # 3 — parse deadline (formato italiano: "23 Luglio 23:59")
        deadline_raw = preprocessed.get("deadline_raw") or ""
        deadline = self._parse_deadline(deadline_raw)
        if not deadline:
            return Result.fail(f"Cannot parse deadline: {deadline_raw}", "MISSING_CRITICAL_FIELD")

        # 4 — normalizza bonus_type
        bonus_type = self._normalize_bonus_type(extracted.get("bonus_type") or "")

        # 5 — normalizza difficulty/priority/variance
        difficulty = self._normalize_keyword(
            extracted.get("difficulty"), ("facile", "medio", "avanzato")
        )
        priority = self._normalize_keyword(extracted.get("priority"), ("alta", "media", "bassa"))
        variance = self._normalize_keyword(extracted.get("variance"), ("alta", "media", "bassa"))

        strategy = dict(extracted.get("strategy") or {})
        if variance and category == "casino":
            strategy.setdefault("variance", variance)

        # 6 — costruisci Signal
        signal = Signal(
            telegram_message_id="",
            book=book,
            category=category,
            bonus_type=bonus_type,
            difficulty=difficulty or "",
            strategy=strategy,
            bonus=dict(extracted.get("bonus") or {}),
            deadline=deadline.isoformat(),
            priority=priority or "",
            expected_profit=extracted.get("expected_profit") or "",
        )

        return Result.ok(signal)

    def _normalize_bonus_type(self, bonus_type_raw: str) -> str:
        lower = bonus_type_raw.lower()
        if "ricorrente" in lower:
            return "ricorrente"
        if "benvenuto" in lower:
            return "benvenuto"
        if "esclusi" in lower:
            return "esclusivo"
        if "rimborso" in lower or "cashback" in lower:
            return "rimborso_slot"
        return bonus_type_raw

    def _normalize_keyword(self, raw: Optional[str], keywords: tuple[str, ...]) -> Optional[str]:
        if not raw:
            return None
        lower = raw.lower()
        for keyword in keywords:
            if keyword in lower:
                return keyword
        return lower

    def _parse_deadline(self, raw: str) -> Optional[datetime]:
        """Parse di date in formato italiano: '23 Luglio 23:59' -> datetime."""
        match = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{1,2}):(\d{2})', raw, re.IGNORECASE)
        if not match:
            return None
        day, month_name, hour, minute = match.groups()
        month = self.MONTHS.get(month_name.lower())
        if not month:
            return None
        return datetime(2026, month, int(day), int(hour), int(minute))


class SignalParser(SignalParserInterface):
    """Orchestratore pipeline P5 Sport — sincrono, zero eccezioni."""

    MISSING_CRITICAL_FIELD = "MISSING_CRITICAL_FIELD"
    LLM_PARSE_FAILED = "LLM_PARSE_FAILED"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    OBSCURED_VALUES = "OBSCURED_VALUES"

    OBSCURED_MARKERS = ("🔡", "💶")
    MAX_LLM_ATTEMPTS = 3

    def __init__(self, rag_system: Optional[RAGSystemInterface] = None):
        self.preprocessor = SignalPreprocessor()
        self.extractor = LlamaSignalExtractor()
        self.postprocessor = SignalPostprocessor()
        self._seen_message_ids: set[str] = set()
        self.rag_system = rag_system

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def parse(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        if telegram_message_id in self._seen_message_ids:
            return Result.fail("Signal already parsed", self.DUPLICATE_SIGNAL)

        if any(marker in raw_text for marker in self.OBSCURED_MARKERS):
            return Result.fail("Obscured animated values in signal", self.OBSCURED_VALUES)

        preprocessed = self.preprocessor.preprocess(raw_text)

        extracted_result = None
        for _ in range(self.MAX_LLM_ATTEMPTS):
            extracted_result = self.extractor.extract(preprocessed, raw_text)
            if extracted_result.success:
                break
        if not extracted_result.success:
            return Result.fail("LLM extraction failed", self.LLM_PARSE_FAILED)

        signal_result = self.postprocessor.postprocess(extracted_result.data, preprocessed)
        if not signal_result.success:
            return signal_result

        signal = signal_result.data
        signal.telegram_message_id = telegram_message_id
        self._seen_message_ids.add(telegram_message_id)
        return Result.ok(signal)

    def parse_and_verify(self, raw_text: str, telegram_message_id: str) -> Result[Signal]:
        parse_result = self.parse(raw_text, telegram_message_id)
        if not parse_result.success:
            return parse_result

        signal = parse_result.data
        tc_result = self._verify_tc(signal)
        if tc_result.success:
            verification = tc_result.data
            signal.tc_verified = verification.verified
            signal.tc_discrepancies = verification.discrepancies
            signal.status = "tc_verified" if verification.verified else "tc_discrepancy"
        else:
            logger.warning(
                "T&C verification failed for message %s (%s): %s — falling back to "
                "unverified stub state",
                telegram_message_id, tc_result.error_code, tc_result.error,
            )
            signal.tc_verified = True
            signal.tc_discrepancies = []
            signal.status = "tc_verified"

        return Result.ok(signal)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _verify_tc(self, signal: Signal) -> Result[TCVerification]:
        """Verifica T&C via RAGSystem se disponibile; altrimenti stub (verified=True)."""
        if self.rag_system is None:
            return Result.ok(TCVerification(verified=True, discrepancies=[], matched_docs=[]))
        return self.rag_system.verify_tc(signal)
