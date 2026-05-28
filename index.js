const express = require('express');
const RSSParser = require('rss-parser');
const fs = require('fs');
const path = require('path');

const app = express();
const parser = new RSSParser();
const PORT = 3000;
const FEEDS_FILE = path.join(__dirname, 'feeds.json');

app.use(express.json());
app.use(express.static('server-ui'));

// Load feeds from disk
function loadFeeds() {
  if (!fs.existsSync(FEEDS_FILE)) {
    const defaults = [
      { name: 'Hacker News', url: 'https://news.ycombinator.com/rss' },
      { name: 'The Verge', url: 'https://www.theverge.com/rss/index.xml' },
    ];
    fs.writeFileSync(FEEDS_FILE, JSON.stringify(defaults, null, 2));
    return defaults;
  }
  return JSON.parse(fs.readFileSync(FEEDS_FILE, 'utf-8'));
}

function saveFeeds(feeds) {
  fs.writeFileSync(FEEDS_FILE, JSON.stringify(feeds, null, 2));
}

// GET /api/feeds — list all configured feeds
app.get('/api/feeds', (req, res) => {
  res.json(loadFeeds());
});

// POST /api/feeds — add a new feed
app.post('/api/feeds', (req, res) => {
  const { name, url } = req.body;
  if (!name || !url) return res.status(400).json({ error: 'name and url required' });
  const feeds = loadFeeds();
  if (feeds.find(f => f.url === url)) return res.status(409).json({ error: 'Feed already exists' });
  feeds.push({ name, url });
  saveFeeds(feeds);
  res.status(201).json({ name, url });
});

// DELETE /api/feeds — remove a feed by url
app.delete('/api/feeds', (req, res) => {
  const { url } = req.body;
  let feeds = loadFeeds();
  feeds = feeds.filter(f => f.url !== url);
  saveFeeds(feeds);
  res.json({ ok: true });
});

// GET /api/articles — fetch & aggregate articles from all feeds
app.get('/api/articles', async (req, res) => {
  const feeds = loadFeeds();
  const results = await Promise.allSettled(
    feeds.map(async (feed) => {
      const parsed = await parser.parseURL(feed.url);
      return parsed.items.map(item => ({
        feedName: feed.name,
        feedUrl: feed.url,
        title: item.title || '(no title)',
        link: item.link || item.guid || '',
        pubDate: item.pubDate || item.isoDate || null,
        summary: item.contentSnippet || item.content || '',
      }));
    })
  );

  const articles = [];
  const errors = [];

  results.forEach((result, i) => {
    if (result.status === 'fulfilled') {
      articles.push(...result.value);
    } else {
      errors.push({ feed: feeds[i].name, error: result.reason.message });
    }
  });

  // Sort newest first
  articles.sort((a, b) => {
    const da = a.pubDate ? new Date(a.pubDate) : 0;
    const db = b.pubDate ? new Date(b.pubDate) : 0;
    return db - da;
  });

  res.json({ articles, errors });
});

app.listen(PORT, () => {
  console.log(`RSS Aggregator running at http://localhost:${PORT}`);
});
