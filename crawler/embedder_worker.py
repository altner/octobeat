"""
embedder_worker.py — Subprocess worker for semantic embeddings.

Runs in an isolated process so torch never shares a process with httpx.
This prevents the macOS 26 fork crash (Network framework + torch threads).

Called by main.py via subprocess; communicates via stdin/stdout JSON.
"""
import os
# Force fully offline: the model is cached locally, so the worker must never
# touch the network. A single HF Hub request would initialize the macOS 26
# Network framework inside this process, and torch's multiprocessing
# resource_tracker fork would then crash (nw_settings_child_has_forked).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("PYTORCH_MPS_ENABLE", "0")

import sys
import json
import logging
from pathlib import Path

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).parent))

import torch
torch.set_num_threads(1)
torch.set_num_interop_threads(1)
if hasattr(torch.backends, "mps"):
    torch.backends.mps.enabled = False

from embedder import build_anchor_embeddings, classify_articles


def main(input_path: str, output_path: str) -> None:
    with open(input_path) as f:
        data = json.load(f)

    articles    = data["articles"]
    tag_anchors = data["tag_anchors"]
    db_path     = Path(data["db_path"]) if data.get("db_path") else None

    anchor_embeddings = build_anchor_embeddings(tag_anchors)
    if not anchor_embeddings:
        with open(output_path, "w") as f:
            f.write("{}")
        os._exit(0)

    results = classify_articles(articles, anchor_embeddings, db_path)

    output = {
        url: [[cat, float(score)] for cat, score in scores]
        for url, scores in results.items()
    }
    with open(output_path, "w") as f:
        json.dump(output, f)

    os._exit(0)  # no GC, no atexit, no torch teardown


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
