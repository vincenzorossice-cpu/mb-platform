#!/usr/bin/env python3
"""
test_kb.py — Test query sulla Knowledge Base ChromaDB
5 query di validazione per verificare la qualità del retrieval.
"""

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "matched_betting_kb"

QUERIES = [
    "Stanleybet ricorrente sport quota minima eventi Champions",
    "Sisal slot rimborso wagering rollover contribuzione",
    "riconciliazione saldo Betfair colonna BANCO differenza",
    "apertura Lottomatica vincoli famiglia Goldbet Planetwin giorni",
    "bonus compleanno scadenza date nascita collaboratori",
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

    for i, query in enumerate(QUERIES, 1):
        results = collection.query(
            query_texts=[query],
            n_results=3,
            include=["documents", "metadatas", "distances"],
        )

        print(f"{'=' * 80}")
        print(f"Query {i}: \"{query}\"")
        print(f"{'=' * 80}")

        ids = results["ids"][0]
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]

        for rank, (doc_id, doc, meta, dist) in enumerate(zip(ids, docs, metas, dists), 1):
            similarity = 1 - dist
            source_id = meta.get("source_id", "?")
            book = meta.get("book", "") or "—"
            category = meta.get("category", "")
            snippet = doc[:100].replace("\n", " ")
            print(
                f"  {rank}. [{source_id}] | book: {book} | cat: {category} "
                f"| sim: {similarity:.3f} | \"{snippet}...\""
            )

        print()

    print("Done. Valuta manualmente se i risultati sono rilevanti per ogni query.")


if __name__ == "__main__":
    main()
