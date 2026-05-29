# OctoBeat

OctoBeat is a link-discovery bot for the German-speaking web. It follows RSS,
blog, Webring, Mastodon, and Bluesky links, learns which domains and curators
surface good finds, and collects the most interesting pieces that are being
linked and discussed.

The bot is the tireless crawler behind the project: it follows links, discovers
what gets referenced repeatedly, scores those signals, and writes static JSON
into `data/`. The Astro frontend reads that data at build time.

## Local Commands

Install dependencies once:

```bash
pip install -r crawler/requirements.txt
npm install
```

Run the bot from the repo root:

```bash
python3 crawler/main.py
```

Start the local frontend:

```bash
npm run dev
```

Open:

```text
http://localhost:4321/octobeat/
```

Build the static site:

```bash
npm run build
```

Preview the built site:

```bash
npm run preview
```

## Bot Schedule

The bot workflow runs daily at 06:17 Europe/Berlin. It intentionally avoids
minute 0 because scheduled GitHub workflows can be delayed at busy times.

## Local Feedback And Ratings

Interactive source and curator ratings only work in local dev. Start the feedback
API in a second terminal:

```bash
npm run feedback
```

Then start the frontend:

```bash
npm run dev
```

Use these local pages:

```text
http://localhost:4321/octobeat/settings/
http://localhost:4321/octobeat/debug/
```

Settings lets you add/delete RSS feed URLs and rate sources from `-5` to `+5`,
reset to `Neutral`, or `Block` them. Debug lets you rate curators. Feedback is
stored in `data/octobeat.sqlite3` and is applied on the next bot run.

## Source List

`data/feeds.json` is the active source list. It contains the RSS feed URLs used
for crawling, and the bot derives the Mastodon/Bluesky search domains from
those feed URLs automatically. There is no separate seed-domain list to maintain.

`crawler/config.yaml` still contains defaults and crawler settings. If
`data/feeds.json` does not exist yet, the bot falls back to the default
`rss_feeds` list in `crawler/config.yaml`.

The bot also learns source quality over time. For each run it stores how
many RSS signals and social signals each feed/domain produced. By default,
inactive sources are only flagged in Settings after the configured run/day
thresholds. Automatic removal stays disabled unless `auto_remove` is set to
`true`.

```yaml
learning:
  source_pruning:
    enabled: true
    auto_remove: false
    min_runs: 5
    min_age_days: 14
    require_no_social_signals: true
```

## Data Flow

```text
data/feeds.json
  -> python3 crawler/main.py
  -> data/feed.json
  -> Astro build/dev frontend
```

The frontend does not fetch JSON at runtime; it reads `data/feed.json` and
`data/feeds.json` during Astro rendering.
