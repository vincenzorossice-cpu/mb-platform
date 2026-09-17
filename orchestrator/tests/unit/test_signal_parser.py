from unittest.mock import MagicMock

import pytest

from orchestrator.core.result import Result
from orchestrator.modules.rag_system.interface import TCVerification
from orchestrator.modules.signal_parser.parser import (
    SignalParser,
    SignalPreprocessor,
)

# ------------------------------------------------------------------
# segnali reali
# ------------------------------------------------------------------

SPORT_STANLEYBET = """📚 STANLEYBET
💎RICORRENTE SPORT PER TUTTI 🚨 LIVELLO FACILE
✅Strategia operativa: Aderisci alla promo e ricarica 100€,
scommetti l'intero deposito in tripla, su Champions League,
Europa League e Conference League, con quota minima 1.8 per evento
e con eventi che terminano entro 3 giorni
✅A refertazione completa, riceverai 25€ di bonus,
da rigiocare in tripla a quota 2 per evento, solo sulle coppe europee
⏰Scadenza: 23 Luglio 23:59
🤩🤩 PRIORITÀ MEDIA
💰💰PROFITTO MEDIO:123€"""

CASINO_SNAI = """📚 SNAI
💎RICORRENTE SLOT PER TUTTI 🚨 LIVELLO FACILE
✅Attiva la promo e gioca 100€ a spin minimi alle slot in promozione,
ricevi il 10% dell'importo giocato sino a massimo 50€ in bonus Real
✋Slot consigliata: Jackpot Bells 10000
⏰Scadenza: 26 luglio 23:59
⚡️⚡️VARIANZA MEDIA
🤩🤩 PRIORITÀ MEDIA
💰VALORE ATTESO: +45€"""

CASINO_SISAL_ADVANCED = """📚SISAL
⏳RICORRENTE SLOT PER TUTTI 🚨 LIVELLO AVANZATO
✅Strategia Operativa: Attiva la Promo.. giocare con saldo reale 100€
a spin minimi alla slot in promozione, si riceve un fun 30€ X40 con cap 30€
📛SLOTO CONSIGLIATA RIGIOCO: King Kong Splash Jackpot King
Strategia:
Margine: Big Bass Bonanza spin 1€
Target: 80€
Rollover: Oink Oink Oink, Stardust oppure Big Bass Bonanza spin 0.10€
⏰Scadenza: 24 Luglio 23:59
🤩🤩🤩 PRIORITA' ALTA
⚡️⚡️MEDIA VARIANZA
💰💰VALORE ATTESO: +60€"""


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

@pytest.fixture
def preprocessor() -> SignalPreprocessor:
    return SignalPreprocessor()


@pytest.fixture
def parser() -> SignalParser:
    return SignalParser()


# ------------------------------------------------------------------
# Step 1 — preprocessing
# ------------------------------------------------------------------

class TestPreprocessSportStanleybet:
    def test_preprocess_sport_stanleybet(self, preprocessor: SignalPreprocessor):
        """Estrae book, bonus_type, deadline, strategy dalla variante sport."""
        fields = preprocessor.preprocess(SPORT_STANLEYBET)
        assert fields["book_raw"] == "STANLEYBET"
        assert "RICORRENTE SPORT" in fields["bonus_type_raw"]
        assert fields["deadline_raw"] == "23 Luglio 23:59"
        assert "tripla" in fields["strategy_raw"]
        assert "coppe europee" in fields["strategy_raw"]


class TestPreprocessCasinoSnai:
    def test_preprocess_casino_snai(self, preprocessor: SignalPreprocessor):
        """Estrae book, bonus_type, slot_advised dalla variante casinò semplice."""
        fields = preprocessor.preprocess(CASINO_SNAI)
        assert fields["book_raw"] == "SNAI"
        assert "RICORRENTE SLOT" in fields["bonus_type_raw"]
        assert "Jackpot Bells 10000" in fields["slot_advised_raw"]


class TestPreprocessCasinoSisal:
    def test_preprocess_casino_sisal(self, preprocessor: SignalPreprocessor):
        """Estrae book, slot_rigioco, target, rollover dalla variante casinò avanzata."""
        fields = preprocessor.preprocess(CASINO_SISAL_ADVANCED)
        assert fields["book_raw"] == "SISAL"
        assert "King Kong Splash Jackpot King" in fields["slot_rigioco_raw"]
        assert "Target: 80€" in fields["slot_rigioco_raw"]
        assert "Rollover:" in fields["slot_rigioco_raw"]


# ------------------------------------------------------------------
# parse completo
# ------------------------------------------------------------------

class TestParseSportFull:
    def test_parse_sport_full(self, parser: SignalParser):
        """Parse completo variante sport -> Signal con category=sport, book=Stanleybet."""
        result = parser.parse(SPORT_STANLEYBET, "msg-001")
        assert result.success
        signal = result.data
        assert signal.category == "sport"
        assert signal.book == "Stanleybet"
        assert signal.bonus_type == "ricorrente"
        assert signal.deadline == "2026-07-23T23:59:00"
        assert signal.strategy["bet_type"] == "tripla"
        assert signal.strategy["leagues"] == [
            "Champions League", "Europa League", "Conference League"
        ]


class TestParseCasinoFull:
    def test_parse_casino_full(self, parser: SignalParser):
        """Parse completo variante casinò semplice -> Signal con category=casino, book=SNAI."""
        result = parser.parse(CASINO_SNAI, "msg-002")
        assert result.success
        signal = result.data
        assert signal.category == "casino"
        assert signal.book == "SNAI"
        assert signal.strategy["slot_advised"] == "Jackpot Bells 10000"


class TestParseCasinoAdvanced:
    def test_parse_casino_advanced(self, parser: SignalParser):
        """Parse completo variante casinò avanzata -> Signal con slot_rigioco, target, rollover."""
        result = parser.parse(CASINO_SISAL_ADVANCED, "msg-003")
        assert result.success
        signal = result.data
        assert signal.category == "casino"
        assert signal.book == "Sisal"
        assert signal.strategy["slot_rigioco"] == "King Kong Splash Jackpot King"
        assert signal.strategy["target"] == pytest.approx(80.0)
        assert "Big Bass Bonanza" in signal.strategy["rollover"]


# ------------------------------------------------------------------
# Step 3 — postprocessing
# ------------------------------------------------------------------

class TestPostprocessBookNormalization:
    def test_postprocess_book_normalization(self, parser: SignalParser):
        """'stanleybet' -> 'Stanleybet'."""
        result = parser.parse(SPORT_STANLEYBET, "msg-004")
        assert result.success
        assert result.data.book == "Stanleybet"


class TestPostprocessDeadlineItalian:
    def test_postprocess_deadline_italian(self, parser: SignalParser):
        """'23 Luglio 23:59' -> '2026-07-23T23:59:00'."""
        result = parser.parse(SPORT_STANLEYBET, "msg-005")
        assert result.success
        assert result.data.deadline == "2026-07-23T23:59:00"


class TestPostprocessBonusTypeNormalization:
    def test_postprocess_bonus_type_normalization(self, parser: SignalParser):
        """'ricorrente sport' -> 'ricorrente'."""
        result = parser.parse(SPORT_STANLEYBET, "msg-006")
        assert result.success
        assert result.data.bonus_type == "ricorrente"


# ------------------------------------------------------------------
# errori
# ------------------------------------------------------------------

class TestMissingBook:
    def test_missing_book(self, parser: SignalParser):
        """Segnale senza 📚 -> MISSING_CRITICAL_FIELD."""
        text_without_book = SPORT_STANLEYBET.replace("📚 STANLEYBET\n", "")
        result = parser.parse(text_without_book, "msg-007")
        assert not result.success
        assert result.error_code == "MISSING_CRITICAL_FIELD"


class TestMissingDeadline:
    def test_missing_deadline(self, parser: SignalParser):
        """Segnale senza ⏰ -> MISSING_CRITICAL_FIELD."""
        text_without_deadline = SPORT_STANLEYBET.replace("⏰Scadenza: 23 Luglio 23:59\n", "")
        result = parser.parse(text_without_deadline, "msg-008")
        assert not result.success
        assert result.error_code == "MISSING_CRITICAL_FIELD"


class TestDuplicateSignal:
    def test_duplicate_signal(self, parser: SignalParser):
        """Stesso telegram_message_id -> DUPLICATE_SIGNAL alla seconda chiamata."""
        first = parser.parse(SPORT_STANLEYBET, "msg-duplicate")
        assert first.success

        second = parser.parse(SPORT_STANLEYBET, "msg-duplicate")
        assert not second.success
        assert second.error_code == "DUPLICATE_SIGNAL"


class TestObscuredValues:
    def test_obscured_values(self, parser: SignalParser):
        """'🔡💶' nel testo -> OBSCURED_VALUES."""
        obscured_text = SPORT_STANLEYBET.replace("100€", "🔡💶")
        result = parser.parse(obscured_text, "msg-009")
        assert not result.success
        assert result.error_code == "OBSCURED_VALUES"


# ------------------------------------------------------------------
# category detection
# ------------------------------------------------------------------

class TestCategoryDetectionSport:
    def test_category_detection_sport(self, parser: SignalParser):
        """Strategia con 'tripla' -> category=sport."""
        result = parser.parse(SPORT_STANLEYBET, "msg-010")
        assert result.success
        assert result.data.category == "sport"


class TestCategoryDetectionCasino:
    def test_category_detection_casino(self, parser: SignalParser):
        """Strategia con 'slot' -> category=casino."""
        result = parser.parse(CASINO_SNAI, "msg-011")
        assert result.success
        assert result.data.category == "casino"


# ------------------------------------------------------------------
# parse_and_verify — T&C verification via RAGSystem
# ------------------------------------------------------------------

class TestParseAndVerifyNoRagSystem:
    def test_parse_and_verify_stub_without_rag_system(self, parser: SignalParser):
        """Senza rag_system (None) -> comportamento stub: tc_verified=True."""
        result = parser.parse_and_verify(SPORT_STANLEYBET, "msg-020")
        assert result.success
        signal = result.data
        assert signal.tc_verified is True
        assert signal.tc_discrepancies == []
        assert signal.status == "tc_verified"


class TestParseAndVerifyRagVerified:
    def test_parse_and_verify_rag_verified(self):
        """RAGSystem.verify_tc -> verified=True -> Signal.status=tc_verified."""
        rag_system = MagicMock()
        rag_system.verify_tc.return_value = Result.ok(
            TCVerification(verified=True, discrepancies=[], matched_docs=["TC-STANLEYBET"])
        )
        parser = SignalParser(rag_system=rag_system)

        result = parser.parse_and_verify(SPORT_STANLEYBET, "msg-021")
        assert result.success
        signal = result.data
        assert signal.tc_verified is True
        assert signal.tc_discrepancies == []
        assert signal.status == "tc_verified"
        rag_system.verify_tc.assert_called_once_with(signal)


class TestParseAndVerifyRagDiscrepancy:
    def test_parse_and_verify_rag_discrepancy(self):
        """RAGSystem.verify_tc -> verified=False -> Signal.status=tc_discrepancy."""
        rag_system = MagicMock()
        rag_system.verify_tc.return_value = Result.ok(
            TCVerification(
                verified=False,
                discrepancies=["Quota anomala"],
                matched_docs=["TC-STANLEYBET"],
            )
        )
        parser = SignalParser(rag_system=rag_system)

        result = parser.parse_and_verify(SPORT_STANLEYBET, "msg-022")
        assert result.success
        signal = result.data
        assert signal.tc_verified is False
        assert signal.tc_discrepancies == ["Quota anomala"]
        assert signal.status == "tc_discrepancy"


class TestParseAndVerifyRagFailureFallsBack:
    def test_parse_and_verify_rag_failure_falls_back_to_stub(self, caplog):
        """RAGSystem.verify_tc -> Result.fail -> non crasha, fallback stub + warning loggato."""
        rag_system = MagicMock()
        rag_system.verify_tc.return_value = Result.fail(
            "Knowledge base vuota — nessun documento indicizzato", "KB_EMPTY"
        )
        parser = SignalParser(rag_system=rag_system)

        with caplog.at_level("WARNING"):
            result = parser.parse_and_verify(SPORT_STANLEYBET, "msg-023")

        assert result.success
        signal = result.data
        assert signal.tc_verified is True
        assert signal.tc_discrepancies == []
        assert signal.status == "tc_verified"
        assert any("KB_EMPTY" in record.message for record in caplog.records)
