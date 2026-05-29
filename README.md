# OctoBeat

German-language social news aggregator. The Python crawler collects Mastodon,
Bluesky and RSS signals, scores articles, and writes static JSON into `data/`.
The Astro frontend reads that data at build time.

## Local Commands

Install dependencies once:

```bash
pip install -r crawler/requirements.txt
npm install
```

Run the crawler from the repo root:

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

## Local Feedback And Ratings

Interactive feed and curator ratings only work in local dev. Start the feedback
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

Settings lets you add/delete RSS feed URLs and rate feeds from `-5` to `+5`,
reset to `Neutral`, or `Block` them. Debug lets you rate curators. Feedback is
stored in `data/octobeat.sqlite3` and is applied on the next crawler run.

## Data Flow

```text
crawler/config.yaml
  -> python3 crawler/main.py
  -> data/feed.json
  -> data/feeds.json
  -> Astro build/dev frontend
```

The frontend does not fetch JSON at runtime; it reads `data/feed.json` and
`data/feeds.json` during Astro rendering.
