# OctoBeat

OctoBeat is a German-language social news aggregator for independent blogs and
non-profit media. It crawls Mastodon, Bluesky, and RSS feeds, scores articles
by social signal strength, and produces a ranked feed that a static Astro
frontend reads at build time.

## Setup

**Python (crawler) — requires Python 3.12+:**

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r crawler/requirements.txt
```

**Optional: local LLM for sharper section classification (Apple Silicon only):**

```bash
pip install -r crawler/requirements-ml.txt   # adds mlx-lm (~60 MB + ~5 GB model on first run)
# Enable in crawler/config.yaml: llm.enabled: true
```

**Node (frontend):**

```bash
npm install
```

## Local Development

Run the crawler to generate `data/feed.json`:

```bash
source .venv/bin/activate   # if not already active
python crawler/main.py
```

Start the dev server:

```bash
npm run dev
# → http://localhost:4321/octobeat/
```

Build and preview the static site:

```bash
npm run build
npm run preview
```

## Deploy

Push `data/feed.json` to `main` — GitHub Actions picks it up, builds the Astro
site, and deploys to GitHub Pages automatically (~2 minutes, no Python in CI).

```bash
git add data/feed.json data/feeds.json data/computed.json data/octobeat.sqlite3
git commit -m "feed: $(date +%Y-%m-%d)"
git push
```

## Feed sections

The feed page groups articles into sections:

| Section | Source |
|---------|--------|
| Category sections (`#ki`, `#gaming`, `#umwelt` …) | Socially shared articles ranked by signal score |
| **Fresh / Undiscovered** | RSS-only articles not yet shared on Mastodon or Bluesky |

## Data Flow

```text
crawler/sources.yaml  ──┐
crawler/config.yaml   ──┤
.env (secrets)        ──┴──▶  python crawler/main.py
                                    │
                     ┌──────────────┤
                     ▼              ▼
              data/feed.json   data/octobeat.sqlite3
                     │
              Astro build ──▶ GitHub Pages
```

The Astro frontend reads `data/feed.json` and `data/feeds.json` at build time —
no client-side JSON fetch in production.

## Configuration

| File | Purpose |
|------|---------|
| `crawler/sources.yaml` | RSS feeds + Mastodon instances |
| `crawler/config.yaml` | Scoring, filters, tag rules, LLM settings |
| `crawler/corrections.yaml` | Manual tag overrides |
| `.env` | `MASTODON_TOKEN`, `MASTODON_INSTANCE`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` |

## Secrets (GitHub Actions)

Add these under **Settings → Secrets → Actions**:

- `MASTODON_TOKEN`
- `MASTODON_INSTANCE`
- `BLUESKY_HANDLE`
- `BLUESKY_APP_PASSWORD`

Set **Settings → Pages → Source** to **GitHub Actions**.

## Requirements

- macOS with Apple Silicon (M1/M2/M3/M4) for local LLM (`llm.enabled: true`)
- Python 3.12+ (system Python 3.9 is not supported — use Homebrew)
- Node 18+
