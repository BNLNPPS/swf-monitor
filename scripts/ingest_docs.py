#!/usr/bin/env python3
"""Ingest documentation into ChromaDB for RAG-based search.

Reads a YAML config listing doc directories, chunks the files,
embeds with sentence-transformers, and stores in a local ChromaDB.

Usage:
    python scripts/ingest_docs.py                  # incremental (skip unchanged)
    python scripts/ingest_docs.py --rebuild         # wipe and re-ingest everything
    python scripts/ingest_docs.py --config alt.yaml # use alternate config
    python scripts/ingest_docs.py --stats           # show collection stats only
"""

import argparse
import glob
import hashlib
import json
import os
import sys
import time

# ChromaDB requires sqlite3 >= 3.35; RHEL8 ships 3.26.
# pysqlite3-binary bundles a modern sqlite3 — swap it in before chromadb loads.
try:
    __import__("pysqlite3")
    sys.modules["sqlite3"] = sys.modules.pop("pysqlite3")
except ImportError:
    pass

import yaml

# Defaults
DEFAULT_CONFIG = os.path.join(os.path.dirname(__file__), "ingest_docs.yaml")
DEFAULT_CHUNK_SIZE = 3000
DEFAULT_CHUNK_OVERLAP = 300
DEFAULT_MODEL = "all-MiniLM-L6-v2"


def chunk_text(text, chunk_size, overlap):
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
        if start >= len(text):
            break
    return chunks


def file_hash(path):
    """Return hex digest of file content + mtime for change detection."""
    mtime = str(os.path.getmtime(path))
    content = open(path, "rb").read()
    return hashlib.sha256(content + mtime.encode()).hexdigest()


def load_config(config_path):
    with open(config_path) as f:
        return yaml.safe_load(f)


def ingest(config_path, rebuild=False, stats_only=False):
    cfg = load_config(config_path)

    chroma_path = os.path.expanduser(cfg.get("chroma_path", "./chroma_db"))
    collection_name = cfg.get("collection", "bamboo_docs")
    chunk_size = cfg.get("chunk_size", DEFAULT_CHUNK_SIZE)
    chunk_overlap = cfg.get("chunk_overlap", DEFAULT_CHUNK_OVERLAP)
    model_name = cfg.get("embedding_model", DEFAULT_MODEL)
    sources = cfg.get("sources", [])

    # Lazy imports — these are heavy
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    ef = SentenceTransformerEmbeddingFunction(model_name)
    client = chromadb.PersistentClient(path=chroma_path)

    if stats_only:
        try:
            col = client.get_collection(collection_name, embedding_function=ef)
            print(f"Collection: {collection_name}")
            print(f"Path: {chroma_path}")
            print(f"Documents: {col.count()}")
            # Show source breakdown
            results = col.get(include=["metadatas"])
            labels = {}
            for m in results["metadatas"]:
                lbl = m.get("source", "unknown")
                labels[lbl] = labels.get(lbl, 0) + 1
            print("By source:")
            for lbl, count in sorted(labels.items()):
                print(f"  {lbl}: {count} chunks")
        except Exception as e:
            print(f"No collection found: {e}")
        return

    if rebuild:
        try:
            client.delete_collection(collection_name)
            print(f"Deleted existing collection '{collection_name}'")
        except chromadb.errors.NotFoundError:
            pass

    collection = client.get_or_create_collection(
        collection_name, embedding_function=ef,
    )

    # Load existing hashes for incremental mode
    existing = {}
    if not rebuild:
        results = collection.get(include=["metadatas"])
        for i, meta in enumerate(results["metadatas"]):
            fpath = meta.get("file_path", "")
            if fpath:
                existing[fpath] = meta.get("file_hash", "")

    total_chunks = 0
    total_files = 0
    skipped = 0

    for source in sources:
        src_path = os.path.expanduser(source["path"])
        pattern = source.get("glob", "**/*.md")
        label = source.get("label", os.path.basename(src_path))

        if not os.path.isdir(src_path):
            print(f"  SKIP {src_path} (not found)")
            continue

        files = sorted(glob.glob(os.path.join(src_path, pattern), recursive=True))
        print(f"[{label}] {src_path}: {len(files)} files")

        for fpath in files:
            fhash = file_hash(fpath)

            # Skip unchanged files in incremental mode
            if not rebuild and existing.get(fpath) == fhash:
                skipped += 1
                continue

            # Delete old chunks for this file before re-adding
            if not rebuild and fpath in existing:
                old_ids = [
                    results["ids"][i]
                    for i, m in enumerate(results["metadatas"])
                    if m.get("file_path") == fpath
                ]
                if old_ids:
                    collection.delete(ids=old_ids)

            text = open(fpath, encoding="utf-8", errors="replace").read()
            if not text.strip():
                continue

            chunks = chunk_text(text, chunk_size, chunk_overlap)
            rel_path = os.path.relpath(fpath, src_path)

            ids = []
            documents = []
            metadatas = []
            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(
                    f"{label}:{rel_path}:{i}".encode()
                ).hexdigest()
                ids.append(doc_id)
                documents.append(chunk)
                metadatas.append({
                    "source": label,
                    "file_path": fpath,
                    "rel_path": rel_path,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "file_hash": fhash,
                })

            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            total_chunks += len(chunks)
            total_files += 1
            print(f"  {rel_path}: {len(chunks)} chunks")

    print(f"\nDone: {total_files} files, {total_chunks} chunks indexed"
          f" ({skipped} unchanged files skipped)")
    print(f"Collection '{collection_name}': {collection.count()} total chunks")


def main():
    parser = argparse.ArgumentParser(description="Ingest docs into ChromaDB")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="YAML config file")
    parser.add_argument("--rebuild", action="store_true",
                        help="Wipe collection and re-ingest everything")
    parser.add_argument("--stats", action="store_true",
                        help="Show collection stats only")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config not found: {args.config}")
        sys.exit(1)

    ingest(args.config, rebuild=args.rebuild, stats_only=args.stats)


if __name__ == "__main__":
    main()
