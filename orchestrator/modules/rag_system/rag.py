import re
from typing import Optional

import chromadb
import yaml
from chromadb.utils import embedding_functions

from orchestrator.core.result import Result
from orchestrator.modules.rag_system.interface import (
    RAGResponse,
    RAGSystemInterface,
    TCVerification,
)
from orchestrator.modules.signal_parser.interface import Signal


class RAGSystem(RAGSystemInterface):
    """Sistema RAG sincrono su ChromaDB — query semantica KB e verifica T&C."""

    CHROMA_CONNECTION_FAILED = "CHROMA_CONNECTION_FAILED"
    KB_QUERY_ERROR = "KB_QUERY_ERROR"
    KB_INDEX_ERROR = "KB_INDEX_ERROR"
    KB_EMPTY = "KB_EMPTY"

    MIN_CONFIDENCE = 0.85          # soglia auto_execute (non ancora raggiungibile, vedi query())
    MIN_SIMILARITY = 0.5           # sotto questa soglia -> no_match
    CHUNK_SIZE = 500
    CHUNK_OVERLAP = 100

    KNOWN_LEAGUES = [
        "Champions League", "Europa League", "Conference League",
        "Serie A", "Serie B", "Premier League", "Liga", "Bundesliga", "Ligue 1",
    ]

    def __init__(self, chroma_path: str = "./chroma_db", collection_name: str = "matched_betting_kb"):
        self.collection = None
        self._init_error: Optional[str] = None
        self._llm = None  # lazy init per Llama 3 (stub per ora)
        try:
            self.chroma = chromadb.PersistentClient(path=chroma_path)
            self.embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="paraphrase-multilingual-mpnet-base-v2"
            )
            self.collection = self.chroma.get_or_create_collection(
                name=collection_name,
                embedding_function=self.embed_fn,
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as exc:
            self._init_error = str(exc)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def query(self, question: str, context: dict,
              process: Optional[int] = None,
              book: Optional[str] = None) -> Result[RAGResponse]:
        """Interroga la KB e restituisce una raccomandazione con confidence e sorgenti."""
        guard = self._ensure_ready()
        if guard is not None:
            return guard

        where_filter: dict = {}
        if process:
            where_filter["process"] = process
        if book:
            where_filter["book"] = book

        res = self._safe_query(question, where_filter if where_filter else None, n_results=5)
        if not res.success:
            return res  # type: ignore[return-value]
        results = res.data

        # fallback: se 0 risultati con filtro, riprova senza filtro
        if not results["documents"][0] and where_filter:
            res = self._safe_query(question, None, n_results=5)
            if not res.success:
                return res  # type: ignore[return-value]
            results = res.data

        if not results["documents"][0]:
            return Result.ok(RAGResponse(
                recommendation="Nessun documento rilevante trovato.",
                confidence=0.0, sources=[], action="no_match",
            ))

        distances = results["distances"][0]
        best_similarity = 1 - distances[0]

        if best_similarity < self.MIN_SIMILARITY:
            return Result.ok(RAGResponse(
                recommendation="Nessun caso simile sufficientemente rilevante.",
                confidence=round(best_similarity, 3),
                sources=[m.get("source_id", "") for m in results["metadatas"][0]],
                action="no_match",
            ))

        retrieved_docs = results["documents"][0]
        recommendation = self._build_stub_recommendation(question, retrieved_docs, context)

        # TODO: quando _llm sarà implementato, valutare auto_execute per confidence molto alta.
        # Oggi nessun LLM decide, quindi action resta sempre hitl_confirm (default sicuro).
        action = "hitl_confirm"
        for m in results["metadatas"][0]:
            if m.get("requires_hitl", False):
                action = "hitl_confirm"
                break

        return Result.ok(RAGResponse(
            recommendation=recommendation,
            confidence=round(best_similarity, 3),
            sources=[m.get("source_id", "") for m in results["metadatas"][0]],
            action=action,
        ))

    def verify_tc(self, signal: Signal) -> Result[TCVerification]:
        """Verifica la plausibilità dei parametri del segnale contro i T&C indicizzati per il book.

        I valori riportati nelle guide (es. "quota minima 1.8") sono esempi operativi, non
        soglie fisse: quote minime, importi e finestre variano per ogni promozione. Non si
        confrontano quindi i valori del segnale con quelli della guida — si verifica solo che,
        per ogni parametro che la guida menziona, il valore del segnale sia plausibile (es.
        quota > 1.0, importo e finestra positivi). Le discrepanze scattano solo su valori
        chiaramente anomali, mai su una semplice differenza numerica dalla guida.
        """
        guard = self._ensure_ready()
        if guard is not None:
            return guard

        query_text = f"{signal.book} {signal.bonus_type} termini condizioni regole vincoli"

        res = self._safe_query(query_text, {"book": signal.book}, n_results=3)
        if not res.success:
            return res  # type: ignore[return-value]
        results = res.data

        # fallback: se 0 risultati con filtro book, riprova senza filtro
        if not results["documents"][0]:
            res = self._safe_query(query_text, None, n_results=3)
            if not res.success:
                return res  # type: ignore[return-value]
            results = res.data

        if not results["documents"][0]:
            return Result.ok(TCVerification(
                verified=False,
                discrepancies=["Nessun documento T&C trovato per questo book/bonus_type"],
                matched_docs=[],
            ))

        doc_text = " ".join(results["documents"][0])
        expected_params = self._extract_tc_params(doc_text, signal)

        # NB: expected_params qui indica solo QUALI parametri la guida menziona (non i loro
        # valori esatti, che sono esempi). Il controllo è di plausibilità sul valore del segnale.
        discrepancies = []

        if signal.category == "sport":
            if "min_odds" in expected_params:
                signal_min_odds = signal.strategy.get("min_odds")
                if signal_min_odds is not None and signal_min_odds <= 1.0:
                    discrepancies.append(
                        f"Quota minima anomala nel segnale: {signal_min_odds} (deve essere > 1.0)"
                    )

            if "events_window_days" in expected_params:
                signal_window = signal.strategy.get("events_window_days")
                if signal_window is not None and signal_window <= 0:
                    discrepancies.append(
                        f"Finestra eventi anomala nel segnale: {signal_window} giorni "
                        "(deve essere positiva)"
                    )

        if "amount" in expected_params:
            signal_amount = signal.strategy.get("amount")
            if signal_amount is not None and signal_amount <= 0:
                discrepancies.append(
                    f"Importo anomalo nel segnale: {signal_amount} EUR (deve essere positivo)"
                )

        matched_docs = [m.get("source_id", "") for m in results["metadatas"][0] if m.get("source_id")]

        return Result.ok(TCVerification(
            verified=len(discrepancies) == 0,
            discrepancies=discrepancies,
            matched_docs=matched_docs,
        ))

    def index_document(self, file_path: str) -> Result[bool]:
        """Indicizza (o rimuove se deprecated) un documento markdown nella KB."""
        if self.collection is None:
            return Result.fail(
                f"ChromaDB non raggiungibile: {self._init_error}", self.CHROMA_CONNECTION_FAILED
            )

        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            return Result.fail(f"Impossibile leggere {file_path}: {exc}", self.KB_INDEX_ERROR)

        meta, body = self._parse_frontmatter(content)
        doc_id = meta.get("id")
        if not doc_id:
            return Result.fail("Campo 'id' mancante nel frontmatter", self.KB_INDEX_ERROR)

        status = meta.get("status", "active")
        superseded_by = meta.get("superseded_by")

        try:
            if status == "deprecated" or superseded_by:
                self.collection.delete(where={"source_id": doc_id})
                return Result.ok(True)

            # idempotenza: cancella i vecchi chunk prima di re-indicizzare
            self.collection.delete(where={"source_id": doc_id})

            chunks = self._chunk_text(body)
            if not chunks:
                return Result.fail(f"Nessun contenuto dopo frontmatter: {doc_id}", self.KB_INDEX_ERROR)

            ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
            metadatas = [{
                "source_id": doc_id,
                "process": int(meta.get("process", 0)),
                "category": str(meta.get("category", "")),
                "bonus_type": str(meta.get("bonus_type", "")),
                "book": str(meta.get("book", "") or ""),
                "chunk_index": i,
            } for i in range(len(chunks))]

            self.collection.add(ids=ids, documents=chunks, metadatas=metadatas)
        except Exception as exc:
            return Result.fail(str(exc), self.KB_INDEX_ERROR)

        return Result.ok(True)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> Optional[Result]:
        """Guard comune a query/verify_tc: collection raggiungibile e non vuota."""
        if self.collection is None:
            return Result.fail(
                f"ChromaDB non raggiungibile: {self._init_error}", self.CHROMA_CONNECTION_FAILED
            )
        try:
            count = self.collection.count()
        except Exception as exc:
            return Result.fail(str(exc), self.KB_QUERY_ERROR)
        if count == 0:
            return Result.fail("Knowledge base vuota — nessun documento indicizzato", self.KB_EMPTY)
        return None

    def _safe_query(self, query_text: str, where_filter: Optional[dict], n_results: int) -> Result[dict]:
        """Wrappa collection.query mappando le eccezioni infrastrutturali a KB_QUERY_ERROR."""
        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=n_results,
                where=where_filter,
            )
        except Exception as exc:
            return Result.fail(str(exc), self.KB_QUERY_ERROR)
        return Result.ok(results)

    def _extract_tc_params(self, doc_text: str, signal: Signal) -> dict:
        """Estrae parametri T&C attesi dal testo dei documenti recuperati via regex."""
        params: dict = {}

        min_odds_match = re.search(
            r"(?:quota\s*(?:minima|min)|min_odds)\s*[:\s]\s*(\d+(?:[.,]\d+)?)",
            doc_text, re.IGNORECASE,
        )
        if min_odds_match:
            params["min_odds"] = float(min_odds_match.group(1).replace(",", "."))

        window_match = re.search(
            r"(?:entro\s+(\d+)\s+giorni|finestra\s*[:\s]\s*(\d+)\s*giorni|window\s*[:\s]\s*(\d+))",
            doc_text, re.IGNORECASE,
        )
        if window_match:
            days = next(g for g in window_match.groups() if g is not None)
            params["events_window_days"] = int(days)

        amount_match = re.search(
            r"(?:ricarica|deposito|gioca|importo)\s*[:\s]*\s*(\d+(?:[.,]\d+)?)\s*€?",
            doc_text, re.IGNORECASE,
        )
        if amount_match:
            params["amount"] = self._clean_italian_amount(amount_match.group(1))

        bet_type_match = re.search(r"\b(tripla|doppia|singola|multipla)\b", doc_text, re.IGNORECASE)
        if bet_type_match:
            params["bet_type"] = bet_type_match.group(1).lower()

        leagues = [lg for lg in self.KNOWN_LEAGUES if lg.lower() in doc_text.lower()]
        if leagues:
            params["leagues"] = leagues

        return params

    def _clean_italian_amount(self, amount_str: str) -> Optional[float]:
        """Normalizza un importo in formato italiano o inglese in un float."""
        s = amount_str.strip()
        s = s.replace("€", "").replace("EUR", "").replace("eur", "").strip()

        negative = False
        if s.startswith("(") and s.endswith(")"):
            negative = True
            s = s[1:-1].strip()
        if s.startswith("-"):
            negative = True
            s = s[1:].strip()
        elif s.startswith("+"):
            s = s[1:].strip()

        has_dot = "." in s
        has_comma = "," in s

        if has_dot and has_comma:
            s = s.replace(".", "").replace(",", ".")
        elif has_comma:
            s = s.replace(",", ".")
        elif has_dot:
            parts = s.split(".")
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                s = s.replace(".", "")

        try:
            value = float(s)
        except ValueError:
            return None

        return -value if negative else value

    def _build_stub_recommendation(self, question: str, docs: list[str], context: dict) -> str:
        """STUB in attesa dell'LLM: ritorna il documento recuperato più rilevante."""
        if not docs:
            return "Nessun documento rilevante trovato."
        summary = docs[0][:500]
        return (
            f"Documenti recuperati dalla KB:\n\n{summary}\n\n"
            "(Nota: raccomandazione LLM non ancora implementata — mostra documento grezzo)"
        )

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """Estrae frontmatter YAML e body dal contenuto markdown."""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
        if not match:
            return {}, content
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}, content
        return meta, match.group(2)

    def _chunk_text(self, text: str) -> list[str]:
        """Sliding window chunker a caratteri (CHUNK_SIZE/CHUNK_OVERLAP)."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + self.CHUNK_SIZE
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            start += self.CHUNK_SIZE - self.CHUNK_OVERLAP
        return chunks
