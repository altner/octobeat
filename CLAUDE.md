# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

OctoBeat is a German-language social news aggregator. It crawls Mastodon, Bluesky, RSS feeds (and optionally Hacker News), scores articles by social signal strength, and produces a ranked `data/feed.json` that a static frontend reads.

## Commands

### Python crawler
```bash
# Install dependencies (run once, from repo root)
pip install -r crawler/requirements.txt

# Run the crawler
python crawler/main.py
```

### Astro frontend
```bash
npm install      # einmalig
npm run dev      # dev server → http://localhost:4321/feedbeat/
npm run build    # statischen Build in dist/ erzeugen
npm run preview  # gebauten Build lokal prüfen
```

### Local Node.js server (live RSS mode)
```bash
npm run server      # production
npm run server:dev  # watch mode
# Server runs at http://localhost:3000
```

## Architecture

There are two independent subsystems that share the `data/` directory:

### 1. Crawler (`crawler/`) — Python pipeline
Runs as a one-shot script (locally or via GitHub Actions daily at 06:00 UTC).

Pipeline: `main.py` orchestrates these steps in order:
1. **Collect** (`collector.py`) — fetches signals from Mastodon, Bluesky, RSS, and optionally HN. Each signal is `{url, platform, curator_handle, curator_meta, shared_at, title?}`.
2. **Filter** — domain blacklist + curator spam filter (`curator.py:is_valid_curator`).
3. **Weight** — `curator.py:calc_weight` derives a float `[0.1, 3.0]` from follower count, follower/following ratio, account age, and platform base.
4. **Enrich** — `collector.py:enrich_titles` fetches `<title>` tags for signals missing a title.
5. **Score** (`scorer.py`) — groups signals by URL, computes: `raw_weight × log2(unique_curators+1) × platform_bonus × 1/(age_hours+2)^1.4 × 1000`.
6. **Filter** — drops articles with no Mastodon/Bluesky signal (RSS-only articles don't make the cut).
7. **Write** (`storage.py`) — writes `data/feed.json` and `data/archive/YYYY-MM-DD.json`.

Config is in `crawler/config.yaml`: seed domains, RSS feed URLs, Bluesky/HN toggles, curator filter thresholds, article filter (max age 48h, top 50), domain blacklist, and output path.

Secrets go in `.env` at the repo root (loaded via `python-dotenv`):
- `MASTODON_TOKEN`, `MASTODON_INSTANCE`
- `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD`

### 2. Local Express server (`index.js`) — Node.js
An alternative local mode that serves a live RSS reader. Separate from the crawler pipeline.
- Serves `public/` as static files
- REST API: `GET/POST/DELETE /api/feeds` (persisted to `feeds.json` in repo root), `GET /api/articles` (live RSS fetch via `rss-parser`)

### 3. Frontend (`src/`) — Astro 5, static output
Three pages built with Astro. Data is read at **build time** from `data/feed.json` and `data/feeds.json` — no client-side JSON fetch. Search uses pre-rendered cards + DOM show/hide.

- `src/pages/index.astro` — main page: sections grouped by hashtag, client-side search
- `src/pages/feeds.astro` — RSS sources list
- `src/pages/debug.astro` — full article list with platform labels, curators, tags
- `src/layouts/Base.astro` — HTML shell, global CSS, header

Sections are built server-side in `index.astro` frontmatter: articles are assigned to their first matching tag that has `≥ 2` articles; unassigned articles → "Weitere".

Config: `astro.config.mjs` — `base: '/feedbeat'` (GitHub Pages sub-path), `build.format: 'file'` (no trailing slashes). Build output → `dist/`.

`server-ui/` contains the local Express server's UI (not part of the Astro build).

### 4. GitHub Actions (`.github/workflows/crawl.yml`)
Single job that crawls, commits data, then deploys to GitHub Pages:
1. Runs `python crawler/main.py` (secrets injected via repo Secrets)
2. Commits `data/feed.json`, `data/feeds.json`, `data/archive/*.json` back to main via `stefanzweifel/git-auto-commit-action` (commits with `GITHUB_TOKEN` do not retrigger the workflow)
3. Copies `index.html`, `feeds.html`, `data/feed.json`, `data/feeds.json` into `_site/` and deploys via `actions/deploy-pages`

When `GITHUB_ACTIONS=true`, `storage.py:push_to_github` is a no-op since the action handles the commit.

**One-time repo setup required:** Go to repo Settings → Pages → Source → set to **"GitHub Actions"**. Then add the secrets `MASTODON_TOKEN`, `MASTODON_INSTANCE`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` under Settings → Secrets → Actions.

## Data flow summary

```
config.yaml → main.py → collector.py → curator.py → scorer.py → storage.py → data/feed.json
                                                                             → data/feeds.json
                                                                             → data/archive/
frontend/index.html ←──────────────────────────────────────────── data/feed.json
```
