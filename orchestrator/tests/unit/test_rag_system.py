from unittest.mock import MagicMock, patch

import pytest

from orchestrator.modules.rag_system.rag import RAGSystem
from orchestrator.modules.signal_parser.interface import Signal


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _make_signal(**overrides) -> Signal:
    defaults = dict(
        telegram_message_id="msg-001",
        book="Stanleybet",
        category="sport",
        bonus_type="ricorrente",
        difficulty="facile",
        strategy={},
        bonus={},
        deadline="2026-07-23T23:59:00",
        priority="media",
        expected_profit="medio",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _query_result(documents, distances, metadatas):
    return {"documents": [documents], "distances": [distances], "metadatas": [metadatas]}


EMPTY_RESULT = _query_result([], [], [])


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------

@pytest.fixture
def rag_system() -> RAGSystem:
    with patch("orchestrator.modules.rag_system.rag.chromadb.PersistentClient"), \
         patch("orchestrator.modules.rag_system.rag.embedding_functions.SentenceTransformerEmbeddingFunction"):
        rs = RAGSystem(chroma_path="/tmp/test_chroma")
    rs.collection = MagicMock()
    rs.collection.count.return_value = 10
    return rs


# ------------------------------------------------------------------
# query
# ------------------------------------------------------------------

class TestQueryWithResults:
    def test_query_with_results(self, rag_system: RAGSystem):
        """3 risultati mockati -> RAGResponse con confidence e sources popolati."""
        rag_system.collection.query.return_value = _query_result(
            ["doc uno", "doc due", "doc tre"],
            [0.3, 0.4, 0.5],
            [
                {"source_id": "P5-SPORT-STANLEYBET-RICORRENTE", "process": 5, "book": "Stanleybet"},
                {"source_id": "DOC-2"},
                {"source_id": "DOC-3"},
            ],
        )
        result = rag_system.query("quota minima stanleybet", {}, process=5, book="Stanleybet")
        assert result.success
        assert result.data.action == "hitl_confirm"
        assert result.data.confidence == pytest.approx(0.7)
        assert result.data.sources == ["P5-SPORT-STANLEYBET-RICORRENTE", "DOC-2", "DOC-3"]


class TestQueryNoResults:
    def test_query_no_results(self, rag_system: RAGSystem):
        """collection.query vuota con e senza filtro -> action=no_match."""
        rag_system.collection.query.return_value = EMPTY_RESULT
        result = rag_system.query("domanda senza risposta", {})
        assert result.success
        assert result.data.action == "no_match"
        assert result.data.confidence == 0.0


class TestQueryLowConfidence:
    def test_query_low_confidence(self, rag_system: RAGSystem):
        """similarity < 0.5 -> action=no_match."""
        rag_system.collection.query.return_value = _query_result(
            ["doc poco rilevante"], [0.6], [{"source_id": "DOC-1"}],
        )
        result = rag_system.query("domanda vaga", {})
        assert result.success
        assert result.data.action == "no_match"
        assert result.data.confidence == pytest.approx(0.4)


class TestQueryFallbackNoFilter:
    def test_query_fallback_no_filter(self, rag_system: RAGSystem):
        """0 risultati con filtro -> riprova senza filtro e usa quei risultati."""
        rag_system.collection.query.side_effect = [
            EMPTY_RESULT,
            _query_result(["doc fallback"], [0.2], [{"source_id": "DOC-FALLBACK"}]),
        ]
        result = rag_system.query("domanda", {}, process=99, book="BookInesistente")
        assert result.success
        assert result.data.sources == ["DOC-FALLBACK"]
        assert rag_system.collection.query.call_count == 2


class TestQueryHighConfidence:
    def test_query_high_confidence(self, rag_system: RAGSystem):
        """similarity > 0.85 -> action resta hitl_confirm (default sicuro, mai auto_execute)."""
        rag_system.collection.query.return_value = _query_result(
            ["doc molto rilevante"], [0.05], [{"source_id": "DOC-1"}],
        )
        result = rag_system.query("domanda molto specifica", {})
        assert result.success
        assert result.data.confidence == pytest.approx(0.95)
        assert result.data.action == "hitl_confirm"


class TestQueryKbEmpty:
    def test_query_kb_empty(self, rag_system: RAGSystem):
        """collection.count()==0 -> Result.fail(KB_EMPTY), nessuna query eseguita."""
        rag_system.collection.count.return_value = 0
        result = rag_system.query("domanda qualsiasi", {})
        assert not result.success
        assert result.error_code == "KB_EMPTY"
        rag_system.collection.query.assert_not_called()


# ------------------------------------------------------------------
# verify_tc
# ------------------------------------------------------------------

class TestVerifyTcVerified:
    def test_verify_tc_verified(self, rag_system: RAGSystem):
        """Quota diversa dall'esempio in guida ('1.8') ma plausibile (>1.0) -> verified=True.

        La guida è un esempio operativo, non una soglia fissa: quote diverse da quella
        indicata non sono di per sé una discrepanza.
        """
        rag_system.collection.query.return_value = _query_result(
            ["Regole Stanleybet: quota minima: 1.8 per evento"],
            [0.1],
            [{"source_id": "TC-STANLEYBET"}],
        )
        signal = _make_signal(strategy={"min_odds": 2.5})
        result = rag_system.verify_tc(signal)
        assert result.success
        assert result.data.verified is True
        assert result.data.discrepancies == []
        assert result.data.matched_docs == ["TC-STANLEYBET"]


class TestVerifyTcDiscrepancyMinOdds:
    def test_verify_tc_discrepancy_min_odds(self, rag_system: RAGSystem):
        """Quota minima anomala nel segnale (<=1.0) -> discrepancy, a prescindere dalla guida."""
        rag_system.collection.query.return_value = _query_result(
            ["Regole Stanleybet: quota minima: 1.8 per evento"],
            [0.1],
            [{"source_id": "TC-STANLEYBET"}],
        )
        signal = _make_signal(strategy={"min_odds": 0.5})
        result = rag_system.verify_tc(signal)
        assert result.success
        assert result.data.verified is False
        assert any("anomala" in d for d in result.data.discrepancies)


class TestVerifyTcNoDocs:
    def test_verify_tc_no_docs(self, rag_system: RAGSystem):
        """Nessun documento trovato con o senza filtro -> verified=False, messaggio dedicato."""
        rag_system.collection.query.return_value = EMPTY_RESULT
        result = rag_system.verify_tc(_make_signal())
        assert result.success
        assert result.data.verified is False
        assert result.data.discrepancies == ["Nessun documento T&C trovato per questo book/bonus_type"]
        assert result.data.matched_docs == []


class TestVerifyTcFallbackNoBookFilter:
    def test_verify_tc_fallback_no_book_filter(self, rag_system: RAGSystem):
        """0 risultati con filtro book -> riprova senza filtro e confronta su quel risultato."""
        rag_system.collection.query.side_effect = [
            EMPTY_RESULT,
            _query_result(
                ["Regole generiche: quota minima: 1.5 per evento"],
                [0.2],
                [{"source_id": "TC-GENERICO"}],
            ),
        ]
        signal = _make_signal(strategy={"min_odds": 1.8})
        result = rag_system.verify_tc(signal)
        assert result.success
        assert result.data.matched_docs == ["TC-GENERICO"]
        assert rag_system.collection.query.call_count == 2


# ------------------------------------------------------------------
# _extract_tc_params
# ------------------------------------------------------------------

class TestExtractTcParamsMinOdds:
    def test_extract_tc_params_min_odds(self, rag_system: RAGSystem):
        """'quota minima: 1.8' -> {'min_odds': 1.8}."""
        params = rag_system._extract_tc_params("quota minima: 1.8", _make_signal())
        assert params["min_odds"] == pytest.approx(1.8)


class TestExtractTcParamsAmount:
    def test_extract_tc_params_amount(self, rag_system: RAGSystem):
        """'ricarica 100€' -> {'amount': 100.0}."""
        params = rag_system._extract_tc_params("ricarica 100€", _make_signal())
        assert params["amount"] == pytest.approx(100.0)


class TestExtractTcParamsWindowDays:
    def test_extract_tc_params_window_days(self, rag_system: RAGSystem):
        """'entro 3 giorni' -> {'events_window_days': 3}."""
        params = rag_system._extract_tc_params("entro 3 giorni", _make_signal())
        assert params["events_window_days"] == 3


# ------------------------------------------------------------------
# _build_stub_recommendation
# ------------------------------------------------------------------

class TestBuildStubRecommendation:
    def test_build_stub_recommendation(self, rag_system: RAGSystem):
        """Ritorna i primi 500 caratteri del doc più rilevante con la nota LLM stub."""
        doc = "A" * 600
        recommendation = rag_system._build_stub_recommendation("domanda", [doc], {})
        assert doc[:500] in recommendation
        assert "raccomandazione LLM non ancora implementata" in recommendation

        empty = rag_system._build_stub_recommendation("domanda", [], {})
        assert empty == "Nessun documento rilevante trovato."


# ------------------------------------------------------------------
# index_document
# ------------------------------------------------------------------

DOC_ACTIVE = """---
id: P5-SPORT-STANLEYBET-RICORRENTE
process: 5
category: sport
bonus_type: ricorrente
book: Stanleybet
status: active
---
Regole operative per la promo ricorrente Stanleybet.
"""

DOC_DEPRECATED = """---
id: P5-SPORT-STANLEYBET-RICORRENTE
process: 5
category: sport
bonus_type: ricorrente
book: Stanleybet
status: deprecated
---
Regole obsolete, non più valide.
"""


class TestIndexDocumentSuccess:
    def test_index_document_success(self, rag_system: RAGSystem, tmp_path):
        """Documento attivo -> delete idempotente + add con i chunk creati."""
        file_path = tmp_path / "doc.md"
        file_path.write_text(DOC_ACTIVE, encoding="utf-8")

        result = rag_system.index_document(str(file_path))

        assert result.success
        assert result.data is True
        rag_system.collection.delete.assert_called_once_with(
            where={"source_id": "P5-SPORT-STANLEYBET-RICORRENTE"}
        )
        rag_system.collection.add.assert_called_once()
        _, kwargs = rag_system.collection.add.call_args
        assert len(kwargs["ids"]) == len(kwargs["documents"]) == len(kwargs["metadatas"])
        assert kwargs["metadatas"][0]["source_id"] == "P5-SPORT-STANLEYBET-RICORRENTE"


class TestIndexDocumentDeprecated:
    def test_index_document_deprecated(self, rag_system: RAGSystem, tmp_path):
        """status=deprecated -> cancella i chunk esistenti e non aggiunge nulla."""
        file_path = tmp_path / "doc.md"
        file_path.write_text(DOC_DEPRECATED, encoding="utf-8")

        result = rag_system.index_document(str(file_path))

        assert result.success
        assert result.data is True
        rag_system.collection.delete.assert_called_once_with(
            where={"source_id": "P5-SPORT-STANLEYBET-RICORRENTE"}
        )
        rag_system.collection.add.assert_not_called()
