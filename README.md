# OctoBeat

OctoBeat is a German-language social news aggregator for independent blogs and
non-profit media. It crawls Mastodon, Bluesky, and RSS feeds, scores articles
by social signal strength, and produces a ranked feed that a static Astro
frontend reads at build time.

## Setup

**Python (crawler):**

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r crawler/requirements.txt
```

**Node (frontend):**

```bash
npm install
```

## Local Development

Run the crawler to generate `data/feed.json`:

```bash
source .venv/bin/activate
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
site, and deploys to GitHub Pages automatically.

```bash
git add data/feed.json data/feeds.json data/computed.json data/octobeat.sqlite3
git commit -m "feed: $(date +%Y-%m-%d)"
git push
```

## Data Flow

```text
crawler/sources.yaml  ──┐
crawler/config.yaml   ──┤
.env (secrets)        ──┴──▶  python crawler/main.py
                                    │
                          data/feed.json  ◀──── Astro build ──▶ GitHub Pages
```

The Astro frontend reads `data/feed.json` and `data/feeds.json` at build time —
no client-side JSON fetch in production.

## Configuration

| File | Purpose |
|------|---------|
| `crawler/sources.yaml` | RSS feeds + Mastodon instances |
| `crawler/config.yaml` | Scoring, filters, tag rules |
| `crawler/corrections.yaml` | Manual tag overrides |
| `.env` | `MASTODON_TOKEN`, `MASTODON_INSTANCE`, `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` |

## Secrets (GitHub Actions)

Add these under **Settings → Secrets → Actions**:

- `MASTODON_TOKEN`
- `MASTODON_INSTANCE`
- `BLUESKY_HANDLE`
- `BLUESKY_APP_PASSWORD`

Set **Settings → Pages → Source** to **GitHub Actions**.
