#!/usr/bin/env python3
"""
index_kb.py — Indicizzazione Knowledge Base in ChromaDB
Legge file .md con frontmatter YAML, splitta in chunk, indicizza con embedding multilingue.
"""

import argparse
import os
import re
import time
import logging
import yaml
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --- Frontmatter parsing ---

def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Estrae frontmatter YAML e body dal contenuto markdown."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, content
    return meta, match.group(2)


# --- Chunking ---

def chunk_text(text: str, size: int = 500, overlap: int = 100) -> list[str]:
    """Sliding window chunker a caratteri."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start += size - overlap
    return chunks


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Indicizza KB markdown in ChromaDB")
    parser.add_argument("--kb-path", required=True, help="Cartella con file .md")
    parser.add_argument("--chroma-path", default="./chroma_db", help="Path persistenza ChromaDB")
    args = parser.parse_args()

    kb_path = os.path.expanduser(args.kb_path)
    chroma_path = os.path.expanduser(args.chroma_path)

    if not os.path.isdir(kb_path):
        logger.error(f"Cartella non trovata: {kb_path}")
        return

    t0 = time.time()

    # Embedding multilingue
    embedding_fn = SentenceTransformerEmbeddingFunction(
        model_name="paraphrase-multilingual-mpnet-base-v2"
    )

    # ChromaDB persistent client
    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_or_create_collection(
        name="matched_betting_kb",
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    # Raccogli tutti i .md
    md_files = []
    for root, _, files in os.walk(kb_path):
        for f in files:
            if f.endswith(".md"):
                md_files.append(os.path.join(root, f))

    docs_read = 0
    docs_skipped = 0
    total_chunks = 0

    for filepath in sorted(md_files):
        docs_read += 1
        rel = os.path.relpath(filepath, kb_path)

        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()

        meta, body = parse_frontmatter(content)

        if not meta:
            logger.warning(f"Frontmatter mancante o invalido, skip: {rel}")
            docs_skipped += 1
            continue

        doc_id = meta.get("id")
        if not doc_id:
            logger.warning(f"Campo 'id' mancante nel frontmatter, skip: {rel}")
            docs_skipped += 1
            continue

        # Skip deprecated
        status = meta.get("status", "active")
        superseded_by = meta.get("superseded_by")
        if status == "deprecated" or superseded_by:
            logger.info(f"Deprecated/superseded, rimuovo e skip: {doc_id}")
            try:
                collection.delete(where={"source_id": doc_id})
            except Exception:
                pass
            docs_skipped += 1
            continue

        # Idempotenza: cancella chunk esistenti
        try:
            collection.delete(where={"source_id": doc_id})
        except Exception:
            pass

        # Chunking
        chunks = chunk_text(body)
        if not chunks:
            logger.warning(f"Nessun contenuto dopo frontmatter: {doc_id}")
            docs_skipped += 1
            continue

        # Prepara batch
        ids = []
        documents = []
        metadatas = []

        tags = meta.get("tags", [])
        tags_str = ",".join(str(t) for t in tags) if isinstance(tags, list) else str(tags or "")

        for i, chunk in enumerate(chunks):
            ids.append(f"{doc_id}_chunk_{i}")
            documents.append(chunk)
            metadatas.append({
                "source_id": doc_id,
                "process": int(meta.get("process", 0)),
                "category": str(meta.get("category", "")),
                "bonus_type": str(meta.get("bonus_type", "")),
                "book": str(meta.get("book", "") or ""),
                "tags": tags_str,
                "chunk_index": i,
                "status": status,
            })

        # Upsert in batch
        collection.add(ids=ids, documents=documents, metadatas=metadatas)
        total_chunks += len(chunks)
        logger.info(f"✓ {doc_id} → {len(chunks)} chunks")

    elapsed = time.time() - t0

    print("\n" + "=" * 50)
    print(f"Documenti letti:    {docs_read}")
    print(f"Documenti saltati:  {docs_skipped}")
    print(f"Documenti indicizzati: {docs_read - docs_skipped}")
    print(f"Chunk totali:       {total_chunks}")
    print(f"Tempo totale:       {elapsed:.1f}s")
    print(f"ChromaDB path:      {chroma_path}")
    print("=" * 50)


if __name__ == "__main__":
    main()
