"""
llm_worker.py — Subprocess worker for local LLM section classification.

Runs mlx-lm in an isolated process (launched via os.posix_spawn from
llm_client.py) so the Metal-backed model never shares a process with httpx.
This avoids the macOS 26 fork crash (Network framework + model threads).

Input/output is exchanged via temp-file paths passed as argv:
    python llm_worker.py <input.json> <output.json>

input  = {"articles": [{"url","title"}], "categories": {cat: hint},
          "model_id": str, "batch_size": int, "max_tokens": int}
output = {url: [category, ...]}   # 0..3 categories per article
"""
import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
import re
import json
import logging
from pathlib import Path

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).parent))


def _build_messages(categories: dict, batch: list[dict]) -> list[dict]:
    cat_lines = "\n".join(f"- {cat}: {hint.strip()}" for cat, hint in categories.items())
    article_lines = "\n".join(f"{i}: {a['title']}" for i, a in enumerate(batch))
    system = (
        "Du bist ein präziser Klassifikator für deutschsprachige Tech- und "
        "News-Artikel. Ordne jeden Artikel den am besten passenden Kategorien zu. "
        "Wähle AUSSCHLIESSLICH aus der vorgegebenen Kategorienliste. Pro Artikel "
        "1 bis 3 Kategorien, oder eine leere Liste, wenn keine wirklich passt. "
        "Antworte NUR mit einem JSON-Objekt, ohne Erklärung."
    )
    user = (
        f"Verfügbare Kategorien:\n{cat_lines}\n\n"
        f"Artikel (Index: Titel):\n{article_lines}\n\n"
        'Antworte als JSON, das jeden Index auf eine Liste von Kategorie-Namen '
        'abbildet, z.B. {"0": ["ki","software"], "1": []}.'
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _parse_json_object(text: str) -> dict:
    """Extract the first JSON object from the model output."""
    # Strip code fences if present
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def main(input_path: str, output_path: str) -> None:
    with open(input_path) as f:
        data = json.load(f)

    articles    = data["articles"]
    categories  = data["categories"]            # {cat: hint}
    model_id    = data["model_id"]
    batch_size  = int(data.get("batch_size", 10))
    max_tokens  = int(data.get("max_tokens", 512))

    allowed = set(categories.keys())
    results: dict[str, list[str]] = {}

    if not articles or not categories:
        with open(output_path, "w") as f:
            json.dump(results, f)
        os._exit(0)

    from mlx_lm import load, generate
    model, tokenizer = load(model_id)

    for start in range(0, len(articles), batch_size):
        batch = articles[start:start + batch_size]
        messages = _build_messages(categories, batch)
        prompt = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        try:
            response = generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens)
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            print(f"  llm batch {start} failed: {type(e).__name__}: {e}", file=sys.stderr)
            continue

        mapping = _parse_json_object(response)
        for idx_str, cats in mapping.items():
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= len(batch):
                continue
            if not isinstance(cats, list):
                continue
            clean = [c for c in cats if isinstance(c, str) and c in allowed]
            if clean:
                results[batch[idx]["url"]] = clean[:3]

    with open(output_path, "w") as f:
        json.dump(results, f)

    os._exit(0)  # no GC, no atexit, no model teardown


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
