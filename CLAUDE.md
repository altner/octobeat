# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

OctoBeat is a German-language social news aggregator focused on independent blogs and non-profit media. It crawls Mastodon (including trending links), Bluesky, and RSS feeds, scores articles by social signal strength, and produces a ranked `data/feed.json` that a static Astro frontend reads.

## Commands

### Python crawler (runs locally — not in CI)
```bash
# Install base dependencies (run once) — includes sentence-transformers/torch
pip install -r crawler/requirements.txt

# Optional: local LLM classification (Apple Silicon, mlx-lm ~60 MB + ~5 GB model)
pip install -r crawler/requirements-ml.txt

# Run the crawler (activate venv first if not already active)
source .venv/bin/activate
python crawler/main.py

# Push results to GitHub (triggers deploy)
git add data/feed.json data/feeds.json data/computed.json data/octobeat.sqlite3
git commit -m "feed: $(date +%Y-%m-%d)"
git push
```

### Astro frontend
```bash
npm install       # once
npm run dev       # dev server → http://localhost:4321/octobeat/
npm run build     # static build → dist/
npm run preview   # preview built output locally
```

## Architecture

Two independent subsystems share the `data/` directory.

### 1. Crawler (`crawler/`) — Python pipeline, runs locally

`main.py` orchestrates these steps:

1. **RSS** (`collector.py:collect_rss`) — fetches feeds from `sources.yaml`; extracts title + RSS `entry.summary` as description
2. **Mastodon Trending** (`collector.py:collect_mastodon_trends`) — fetches trending article links from Fediverse instances via `/api/v1/trends/links`
3. **Mastodon search** (`collector.py:collect_mastodon`) — searches instances for links to RSS article domains (parallel per domain)
4. **Bluesky search** (`collector.py:collect_bluesky`) — same domains on Bluesky (token fetched once, shared across parallel calls)
5. **Filter** — domain blacklist + curator spam filter (`curator.py:is_valid_curator`)
6. **Weight** — `curator.py:calc_weight` derives a float `[0.1, 3.0]` from follower count, ratio, account age, platform base
7. **Enrich titles** (`collector.py:enrich_titles`) — fetches `og:title` / `<title>` for missing titles; also extracts `og:description` in the same request; falls back to URL slug
8. **Group by URL** — signals grouped per article URL; best description (og:description or RSS summary) stored per article
9. **Embeddings** (`embedder.py`) — semantic classification via `paraphrase-multilingual-MiniLM-L12-v2`; cached in SQLite; runs isolated via `os.posix_spawn` (macOS 26 fork-crash fix)
10. **Score** (`scorer.py`) — `raw_weight × log2(unique_curators+1) × platform_bonus × engagement_bonus × time_decay × 1000`
11. **WordPress enrichment** (`collector.py:enrich_wordpress_engagement`) — fetches comment/like counts via WP REST API where available; cached in SQLite
12. **Split** — separates titled articles into social finds (Mastodon/Bluesky signal) and a **fresh pool** (RSS-only, no social signal yet); drops empty/numeric titles
13. **Top N + Fresh** — social finds sorted by score, max `max_per_domain` (default 3) per domain, keep top 50; fresh pool sorted by recency, domain-capped, keep `fresh_n` (default 20)
14. **Tag inference** (`main.py:infer_article_tags`) — weighted layers: `tag_map` (+3), embeddings (+2–4), `tag_rules` keywords (+1), LLM (+`llm.weight`); corrections from DB/YAML override all
15. **Article content fetch** (`collector.py:enrich_article_content`) — when `llm.enabled`, fetches body text (first ~500 chars) for the final feed articles; also backfills missing descriptions
16. **LLM section refinement** (`llm_client.py` → `llm_worker.py`) — when `llm.enabled`, a local MLX model (Qwen2.5-7B-4bit) classifies the final feed using title + URL context + description + body text; runs isolated via `os.posix_spawn`; also emits new-category suggestions for unmatched articles
17. **Auto-category discovery** — LLM suggestions for unknown categories are stored in `category_suggestions` (SQLite); when `min_articles` + `min_runs` threshold is reached, the category is automatically added to `config.yaml` (tag_anchors + tag_map + tag_rules)
18. **SQLite** (`database.py:record_run`) — stores run history, curator stats, tag history, unmapped tags, category suggestions
19. **Export** (`database.py:export_computed`) — writes `data/computed.json` (DB backup/restore source)
20. **Write** (`storage.py:write_feed`) — writes `data/feed.json` (incl. `top_curators` and `fresh` array) and `data/archive/YYYY-MM-DD.json`

### Key config files

| File | Purpose |
|------|---------|
| `crawler/config.yaml` | Technical settings: scoring, filters, embeddings, LLM, tag rules |
| `crawler/sources.yaml` | Community-maintained RSS feeds + Mastodon instances |
| `crawler/corrections.yaml` | Manual tag overrides (URL → [categories]) |
| `crawler/requirements.txt` | Base Python dependencies (includes sentence-transformers) |
| `crawler/requirements-ml.txt` | Optional: mlx-lm for local LLM (Apple Silicon only) |
| `.env` | Secrets: `MASTODON_TOKEN`, `MASTODON_INSTANCE`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` |

### 2. Frontend (`src/`) — Astro 5, static output

Data read at **build time** from `data/feed.json` — no client-side fetch.

| Page | Description |
|------|-------------|
| `src/pages/index.astro` | Main feed: sections by category + "Fresh / Undiscovered" section, client-side search |
| `src/pages/inspector.astro` | All articles with platform labels, curators, tags (read-only in prod) |
| `src/pages/metrics.astro` | Tag classifier stats, curator rankings, unmapped RSS tags |
| `src/pages/settings.astro` | RSS source list (API offline banner in prod, interactive in dev) |
| `src/pages/doc.astro` | Public documentation for visitors |
| `src/layouts/BaseLayout.astro` | HTML shell, global CSS, footer |
| `src/components/Header.astro` | Sticky header, logo, nav links, update timestamp |

### 3. GitHub Actions (`.github/workflows/deploy.yml`)

Triggered on push to `main` when `data/feed.json` changes. Does **not** run the crawler.

1. `npm ci` + `npm run build` (Astro SSG → `dist/`)
2. Deploy to GitHub Pages via `actions/deploy-pages`

**Deploy time: ~2 minutes** (no Python, no crawler).

### 4. GitHub Actions (`.github/workflows/triage-suggestion.yml`)

Triggered when an issue gets labeled `feed-suggestion` or `instance-suggestion`.
Automatically creates a PR adding the suggested URL to `crawler/sources.yaml`.

## Data flow summary

```
sources.yaml ──┐
config.yaml ───┤
               ├─→ main.py → collector.py → scorer.py → storage.py → data/feed.json
.env ──────────┘                │                                   → data/feeds.json
                                ↓                                   → data/computed.json
                  llm_client.py → llm_worker.py (posix_spawn)       → data/archive/
                                ↓
                  database.py → data/octobeat.sqlite3

git push → GitHub Actions (deploy.yml) → npm build → GitHub Pages
```

## Tag classification system

Layers combined by weighted score:

| Source | Weight | Example |
|--------|--------|---------|
| `tag_map` (RSS source tag → category) | +3 | `#hardware` → `hardware` |
| Embedding similarity (semantic) | +2–4 | "Turtle Beach Controller" → `gaming` |
| `tag_rules` keyword match on title/URL | +1 | `xbox` in title → `gaming` |
| Local LLM (`llm.enabled: true`) | +4 (configurable) | uses title + URL + description + body text |
| `corrections.yaml` / DB override | +99 | manual fix |

**Local LLM layer** (`crawler/llm_worker.py` launched via `crawler/llm_client.py`):
- Model: `mlx-community/Qwen2.5-7B-Instruct-4bit` (~5 GB, cached in `~/.cache/huggingface/hub/`)
- Runs **only on the final feed** (top + fresh, ≤ `llm.max_articles`)
- Prompt includes: title, `[domain · url-slug]`, description, first ~400 chars of body text
- Returns existing category assignments + optional new-category suggestions for unmatched articles
- Isolated via `os.posix_spawn` — never forks from the main process (macOS 26 Network-framework fork-crash fix)
- Requires `pip install -r crawler/requirements-ml.txt`; degrades gracefully if missing

**Auto-category discovery** (`llm.auto_categories`):
- When LLM finds no matching category, it suggests a new German keyword
- Suggestions accumulate in `category_suggestions` (SQLite)
- Once `min_articles` (default 3) from `min_runs` (default 2) runs agree → automatically added to `config.yaml`
- Config path: `crawler/config.yaml` → `llm.auto_categories`

## macOS 26 note

macOS 26 crashes any `fork()` call made after the Network framework has been
initialized (e.g. after httpx makes its first request). All subprocess workers
(`embedder_worker.py`, `llm_worker.py`) are launched via `os.posix_spawn`, which
never forks, avoiding the crash entirely. Never use `subprocess.run` with
`capture_output=True` (which forces `fork+exec`) in the crawler's main process.

## SQLite tables (data/octobeat.sqlite3)

- `runs`, `run_articles`, `signals` — crawl history
- `curator_stats`, `curator_feedback` — per-curator learning
- `feed_feedback` — per-feed ratings
- `source_stats`, `source_runs` — feed performance over time
- `tag_history` — tag assignments per run
- `tag_corrections` — manual tag overrides (editable via /metrics)
- `unmapped_tags` — RSS tags not yet in tag_map (feeds /metrics suggestions)
- `article_embeddings` — cached title vectors (model: paraphrase-multilingual-MiniLM-L12-v2)
- `wp_engagement` — WordPress REST API comment/like cache
- `category_suggestions` — LLM-suggested new categories pending promotion to config.yaml
