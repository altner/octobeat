"""
embedder.py — semantic category classification via sentence embeddings.

Uses paraphrase-multilingual-MiniLM-L12-v2 (120MB, runs offline after first download).
Embeddings are cached in SQLite to avoid recomputation.

Integration: called from infer_article_tags, adds weight +2..+4 per category
based on cosine similarity to category anchor texts.
"""

from __future__ import annotations

import json
import struct
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from numpy import ndarray

import os
import logging
import warnings

# Suppress HF Hub unauthenticated-request warning (model is cached locally)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)

warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
SIMILARITY_THRESHOLD = 0.38   # below this → ignore
WEIGHT_SCALE = 4.0            # max weight added at similarity=1.0
TOP_K = 3                     # max categories to emit per article

_model = None


def unload_model() -> None:
    """Release model from memory after all embeddings are computed.
    Call this before process exit to avoid torch atexit/fork crash on macOS."""
    global _model
    if _model is not None:
        del _model
        _model = None
        import gc
        gc.collect()


def _get_model():
    global _model
    if _model is None:
        import torch
        torch.set_num_threads(1)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def _to_blob(vec) -> bytes:
    data = vec.tolist() if hasattr(vec, "tolist") else list(vec)
    return struct.pack(f"{len(data)}f", *data)


def _from_blob(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_texts(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


def build_anchor_embeddings(anchors: dict[str, str]) -> dict[str, list[float]]:
    """Compute embeddings for category anchor texts. Call once per run."""
    if not anchors:
        return {}
    cats   = list(anchors.keys())
    texts  = [anchors[c] for c in cats]
    vecs   = _embed_texts(texts)
    return dict(zip(cats, vecs))


def load_cached_embedding(db_path: Path, url: str) -> list[float] | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT embedding, model FROM article_embeddings WHERE url = ?", (url,)
    ).fetchone()
    con.close()
    if row and row[1] == MODEL_NAME:
        return _from_blob(row[0])
    return None


def save_embedding(db_path: Path, url: str, vec: list[float]) -> None:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=DELETE")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO article_embeddings(url, model, embedding, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
          model = excluded.model,
          embedding = excluded.embedding,
          updated_at = excluded.updated_at
        """,
        (url, MODEL_NAME, _to_blob(vec), now),
    )
    con.commit()
    con.close()


def classify_articles(
    articles: list[dict],
    anchor_embeddings: dict[str, list[float]],
    db_path: Path | None = None,
) -> dict[str, list[tuple[str, float]]]:
    """
    For each article, return [(category, similarity), ...] sorted desc.
    Only includes categories above SIMILARITY_THRESHOLD.
    Results keyed by article URL.
    """
    if not anchor_embeddings:
        return {}

    # Collect articles that need embedding (not cached)
    to_embed: list[tuple[str, str]] = []  # (url, title)
    cached:   dict[str, list[float]] = {}

    for a in articles:
        url   = a["url"]
        title = a.get("title") or url
        if db_path:
            vec = load_cached_embedding(db_path, url)
            if vec:
                cached[url] = vec
                continue
        to_embed.append((url, title))

    # Batch-compute missing embeddings
    if to_embed:
        urls, titles = zip(*to_embed)
        vecs = _embed_texts(list(titles))
        for url, vec in zip(urls, vecs):
            cached[url] = vec
            if db_path:
                save_embedding(db_path, url, vec)

    # Score each article against all category anchors
    results: dict[str, list[tuple[str, float]]] = {}
    for a in articles:
        url = a["url"]
        vec = cached.get(url)
        if not vec:
            continue
        scores = [
            (cat, _cosine(vec, anchor_vec))
            for cat, anchor_vec in anchor_embeddings.items()
        ]
        scores = [(c, s) for c, s in scores if s >= SIMILARITY_THRESHOLD]
        scores.sort(key=lambda x: x[1], reverse=True)
        results[url] = scores[:TOP_K]

    return results


def classify_articles_subprocess(
    articles: list[dict],
    tag_anchors: dict[str, str],
    db_path: "Path | None" = None,
) -> "dict[str, list[tuple[str, float]]]":
    """Run classification in an isolated subprocess (macOS 26 fork-crash fix).

    torch and httpx never share a process, so the Network-framework fork
    restriction on macOS 26 is never triggered.
    """
    import sys
    import json as _json
    import subprocess

    worker = Path(__file__).parent / "embedder_worker.py"
    payload = _json.dumps({
        "articles":    articles,
        "tag_anchors": tag_anchors,
        "db_path":     str(db_path) if db_path else None,
    })

    try:
        result = subprocess.run(
            [sys.executable, str(worker)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout.strip():
            raw = _json.loads(result.stdout.strip())
            return {url: [(cat, score) for cat, score in scores] for url, scores in raw.items()}
        if result.stderr:
            print(f"  embedding worker: {result.stderr[:300]}", file=sys.stderr)
        return {}
    except subprocess.TimeoutExpired:
        print("  embedding worker timed out — skipping", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"  embedding worker failed: {e}", file=sys.stderr)
        return {}
