# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

OctoBeat is a German-language social news aggregator focused on independent blogs and non-profit media. It crawls Mastodon (including trending links), Bluesky, and RSS feeds, scores articles by social signal strength, and produces a ranked `data/feed.json` that a static Astro frontend reads.

## Commands

### Python crawler (runs locally — not in CI)
```bash
# Install base dependencies (run once)
pip3 install -r crawler/requirements.txt
# Optional: ML-based tag classifier (pulls in torch ~73 MB — skip for CI)
pip3 install -r crawler/requirements-ml.txt

# Run the crawler
python3 crawler/main.py

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

1. **RSS** (`collector.py:collect_rss`) — fetches 13 independent German blogs/non-profits from `sources.yaml`
2. **Mastodon Trending** (`collector.py:collect_mastodon_trends`) — fetches trending article links from 8 Fediverse instances via `/api/v1/trends/links`
3. **Mastodon search** (`collector.py:collect_mastodon`) — searches instances for links to RSS article domains (parallel per domain)
4. **Bluesky search** (`collector.py:collect_bluesky`) — same domains on Bluesky (token fetched once, shared across parallel calls)
5. **Filter** — domain blacklist + curator spam filter (`curator.py:is_valid_curator`)
6. **Weight** — `curator.py:calc_weight` derives a float `[0.1, 3.0]` from follower count, ratio, account age, platform base
7. **Enrich titles** — `collector.py:enrich_titles` fetches `<title>`/`og:title` for missing titles; falls back to URL slug for useless titles like "status"
8. **Group by URL** — signals grouped per article URL
9. **Embeddings** (`embedder.py`) — semantic classification via `paraphrase-multilingual-MiniLM-L12-v2`; embeddings cached in SQLite
10. **Score** (`scorer.py`) — `raw_weight × log2(unique_curators+1) × platform_bonus × engagement_bonus × time_decay × 1000`
11. **WordPress enrichment** (`collector.py:enrich_wordpress_engagement`) — fetches comment/like counts via WP REST API where available; cached in SQLite
12. **Filter** — drops articles without Mastodon/Bluesky signal, empty titles, numeric-only titles
13. **Top N** — sort by score, max `max_per_domain` (default 3) per domain, keep top 50
14. **Tag inference** (`main.py:infer_article_tags`) — three-layer system: `tag_map` (+3), embeddings (+2–4), `tag_rules` keywords (+1); corrections from DB/YAML override all
15. **SQLite** (`database.py:record_run`) — stores run history, curator stats, tag history, unmapped tags
16. **Export** (`database.py:export_computed`) — writes `data/computed.json` (DB backup/restore source)
17. **Write** (`storage.py:write_feed`) — writes `data/feed.json` (incl. `top_curators`) and `data/archive/YYYY-MM-DD.json`

### Key config files

| File | Purpose |
|------|---------|
| `crawler/config.yaml` | Technical settings: scoring, filters, embeddings, tag rules |
| `crawler/sources.yaml` | Community-maintained RSS feeds + Mastodon instances |
| `crawler/corrections.yaml` | Manual tag overrides (URL → [categories]) |
| `.env` | Secrets: `MASTODON_TOKEN`, `MASTODON_INSTANCE`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` |

### 2. Frontend (`src/`) — Astro 5, static output

Data read at **build time** from `data/feed.json` — no client-side fetch.

| Page | Description |
|------|-------------|
| `src/pages/index.astro` | Main feed: sections by category, client-side search |
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
.env ──────────┘                                                    → data/feeds.json
                        ↓                                           → data/computed.json
                  database.py → data/octobeat.sqlite3               → data/archive/

git push → GitHub Actions (deploy.yml) → npm build → GitHub Pages
```

## Tag classification system

Three layers, combined by weighted score:

| Source | Weight | Example |
|--------|--------|---------|
| `tag_map` (RSS source tag → category) | +3 | `#hardware` → `hardware` |
| Embedding similarity (semantic) | +2–4 | "Turtle Beach Controller" → `gaming` |
| `tag_rules` keyword match on title/URL | +1 | `xbox` in title → `gaming` |
| `corrections.yaml` / DB override | +99 | manual fix |

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
