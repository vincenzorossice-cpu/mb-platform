#!/usr/bin/env python3
"""
test_kb_filtered.py — Test query con filtri where su ChromaDB
Valida che i filtri per category, book e process migliorino il retrieval.
"""

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "matched_betting_kb"

FILTERED_QUERIES = [
    {
        "label": "Sport Champions — filtro category=sport",
        "query": "quota minima eventi Champions League tripla",
        "where": {"category": "sport"},
    },
    {
        "label": "Slot wagering — filtro category=casino",
        "query": "slot rimborso wagering rollover contribuzione",
        "where": {"category": "casino"},
    },
    {
        "label": "Riconciliazione — filtro process=6",
        "query": "riconciliazione saldo colonna BANCO differenza",
        "where": {"process": 6},
    },
    {
        "label": "Apertura conti — filtro process=2",
        "query": "apertura vincoli famiglia giorni attesa documenti",
        "where": {"process": 2},
    },
    {
        "label": "Bonus compleanno — filtro bonus_type=compleanno",
        "query": "bonus scadenza date nascita collaboratori",
        "where": {"bonus_type": "compleanno"},
    },
    {
        "label": "Stanleybet sport — filtro book=Stanleybet",
        "query": "ricorrente quota minima eventi multipla",
        "where": {"book": "Stanleybet"},
    },
    {
        "label": "Betfair — filtro book=Betfair",
        "query": "bancata lay mercato exchange commissione",
        "where": {"book": "Betfair"},
    },
]


def main():
    embedding_fn = SentenceTransformerEmbeddingFunction(
        model_name="paraphrase-multilingual-mpnet-base-v2"
    )

    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
    )

    print(f"Collection: {COLLECTION_NAME} | Totale chunk: {collection.count()}\n")

    hits = 0
    total = len(FILTERED_QUERIES)

    for i, q in enumerate(FILTERED_QUERIES, 1):
        print(f"{'=' * 80}")
        print(f"Query {i}: \"{q['query']}\"")
        print(f"Filtro: {q['where']}")
        print(f"{'=' * 80}")

        try:
            results = collection.query(
                query_texts=[q["query"]],
                n_results=3,
                where=q["where"],
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            print(f"  ✗ Errore: {e}")
            print(f"  (Possibile: nessun chunk matcha il filtro)\n")
            continue

        ids = results["ids"][0]
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]

        if not ids:
            print("  Nessun risultato per questo filtro.\n")
            continue

        has_relevant = False
        for rank, (doc_id, doc, meta, dist) in enumerate(zip(ids, docs, metas, dists), 1):
            similarity = 1 - dist
            source_id = meta.get("source_id", "?")
            book = meta.get("book", "") or "—"
            category = meta.get("category", "")
            process = meta.get("process", "")
            snippet = doc[:100].replace("\n", " ")

            if similarity > 0.35:
                has_relevant = True

            print(
                f"  {rank}. [{source_id}] | book: {book} | cat: {category} | P{process} "
                f"| sim: {similarity:.3f} | \"{snippet}...\""
            )

        status = "✅ HIT" if has_relevant else "⚠️ WEAK"
        if has_relevant:
            hits += 1
        print(f"  → {status}\n")

    print("=" * 80)
    print(f"RISULTATI: {hits}/{total} query con hit rilevante (sim > 0.35)")
    accuracy = (hits / total * 100) if total else 0
    print(f"Retrieval accuracy con filtri: {accuracy:.0f}%")
    target = "✅ PASS" if accuracy >= 80 else "⚠️ SOTTO TARGET"
    print(f"Target 80%: {target}")
    print("=" * 80)


if __name__ == "__main__":
    main()
