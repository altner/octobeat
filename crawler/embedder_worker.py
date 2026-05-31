"""
embedder_worker.py — Subprocess worker for semantic embeddings.

Runs in an isolated process so torch never shares a process with httpx.
This prevents the macOS 26 fork crash (Network framework + torch threads).

Called by main.py via subprocess; communicates via stdin/stdout JSON.
"""
import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("PYTORCH_MPS_ENABLE", "0")

import sys
import json
import logging
from pathlib import Path

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).parent))

from embedder import build_anchor_embeddings, classify_articles


def main() -> None:
    data        = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    articles    = data["articles"]
    tag_anchors = data["tag_anchors"]
    db_path     = Path(data["db_path"]) if data.get("db_path") else None

    anchor_embeddings = build_anchor_embeddings(tag_anchors)
    if not anchor_embeddings:
        sys.stdout.write("{}\n")
        sys.stdout.flush()
        return

    results = classify_articles(articles, anchor_embeddings, db_path)

    output = {
        url: [[cat, float(score)] for cat, score in scores]
        for url, scores in results.items()
    }
    sys.stdout.write(json.dumps(output) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
    os._exit(0)  # skip torch atexit handlers — all results already written to stdout
