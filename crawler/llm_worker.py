"""
llm_worker.py — Subprocess worker for local LLM section classification.

Runs mlx-lm in an isolated process (launched via os.posix_spawn from
llm_client.py) so the Metal-backed model never shares a process with httpx.
This avoids the macOS 26 fork crash (Network framework + model threads).

Input/output is exchanged via temp-file paths passed as argv:
    python llm_worker.py <input.json> <output.json>

input  = {"articles": [{"url","title","description","content"}], "categories": {cat: hint},
          "model_id": str, "batch_size": int, "max_tokens": int}
output = {url: [category, ...],          # 0..3 existing categories per article
          "_new": {url: "new_category"}} # LLM-suggested new category for unmatched articles
"""
import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sys
import re
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

sys.path.insert(0, str(Path(__file__).parent))

# Words that carry no semantic value in URL slugs
_SLUG_NOISE = frozenset({
    "www", "de", "com", "org", "net", "io", "eu",
    "the", "and", "for", "der", "die", "das", "und",
    "ein", "eine", "ist", "von", "mit", "bei", "zum",
    "zur", "im", "in", "an", "am", "auf", "aus", "als",
    "2024", "2025", "2026", "html", "php", "index", "feed",
    "blog", "post", "page", "p", "en", "de",
})
_DATE_PATTERN = re.compile(r"^\d{4}$|^\d{2}$|^\d{1,2}-\d{1,2}$")


def _url_context(url: str) -> str:
    """Extract domain + meaningful slug keywords from a URL.

    Returns a compact string like:
        "kuketz-blog.de · datenschutz android apps sicherheit"
    which gives the LLM strong topical hints beyond the article title.
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        # Split path into tokens, normalise separators
        raw = re.split(r"[/\-_+.]+", parsed.path.lower())
        keywords = [
            t for t in raw
            if len(t) > 2
            and not _DATE_PATTERN.match(t)
            and not t.isdigit()
            and t not in _SLUG_NOISE
        ]
        hint = " ".join(keywords[:6])  # max 6 slug words
        return f"{domain} · {hint}" if hint else domain
    except Exception:  # noqa: BLE001
        return ""


def _build_messages(categories: dict, batch: list[dict]) -> list[dict]:
    cat_lines = "\n".join(f"- {cat}: {hint.strip()}" for cat, hint in categories.items())
    def _fmt(i: int, a: dict) -> str:
        lines = [f"{i}: {a['title']}"]
        ctx = _url_context(a.get("url", ""))
        if ctx:
            lines[0] += f"  [{ctx}]"
        desc = (a.get("description") or "").strip()
        if desc:
            lines.append(f"   Beschreibung: {desc[:200]}")
        content = (a.get("content") or "").strip()
        if content:
            lines.append(f"   Inhalt: {content[:400]}")
        return "\n".join(lines)

    article_lines = "\n".join(_fmt(i, a) for i, a in enumerate(batch))
    system = (
        "Du bist ein präziser Klassifikator für deutschsprachige Artikel aus unabhängigen Blogs "
        "und Medien. Ordne jeden Artikel den am besten passenden Kategorien zu.\n"
        "Wähle AUSSCHLIESSLICH aus der vorgegebenen Kategorienliste. Pro Artikel 1–3 Kategorien, "
        "oder [] wenn wirklich keine passt.\n\n"
        "Wichtige Regeln:\n"
        "- Persönliche Rückblicke, Alltagsberichte, Fitness-Updates (Laufen, Radfahren, Rudern…) "
        "und Linksammlungen → immer 'blog', NICHT nach Aktivität klassifizieren\n"
        "- 'gaming' NUR für Artikel über Videospiele, Konsolen oder eSport — NICHT für Sport, "
        "Bewegung oder körperliche Aktivitäten\n"
        "- Wenn der Artikel ein persönliches Erlebnis beschreibt (auch wenn Technik erwähnt wird) "
        "→ 'blog'\n"
        "- Antworte NUR mit dem JSON-Objekt, keine Erklärung"
    )
    user = (
        f"Verfügbare Kategorien:\n{cat_lines}\n\n"
        f"Artikel (Index: Titel  [Domain · Slug]  » Beschreibung):\n{article_lines}\n\n"
        "Antworte als JSON mit zwei Schlüsseln:\n"
        '- "classified": Index → Liste bestehender Kategorien (oder [] wenn keine passt)\n'
        '- "new": Index → ein einzelnes deutsches Schlagwort für Artikel, '
        'bei denen KEINE bestehende Kategorie wirklich passt (weglassen wenn nicht nötig)\n\n'
        'Beispiel: {"classified": {"0": ["ki","software"], "1": [], "2": ["gaming"]}, '
        '"new": {"1": "raumfahrt"}}'
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

        raw = _parse_json_object(response)
        # Support both new format {"classified":{...},"new":{...}} and
        # old flat format {"0":[...]} for robustness.
        if "classified" in raw:
            classified = raw.get("classified", {})
            new_cats   = raw.get("new", {})
        else:
            classified = raw
            new_cats   = {}

        for idx_str, cats in classified.items():
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

        for idx_str, suggestion in new_cats.items():
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= len(batch):
                continue
            if not isinstance(suggestion, str) or not suggestion.strip():
                continue
            url = batch[idx]["url"]
            # Only record suggestion if no existing category was assigned
            if url not in results:
                if "_new" not in results:
                    results["_new"] = {}
                results["_new"][url] = suggestion.strip().lower()

    with open(output_path, "w") as f:
        json.dump(results, f)

    os._exit(0)  # no GC, no atexit, no model teardown


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
