"""
collector.py — collect signals from Mastodon, Bluesky, Hacker News, and RSS.
Each signal = {url, platform, curator_handle, curator_meta, shared_at, title?}
"""

import os
import re
import httpx
import feedparser
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs
from datetime import datetime, timezone
from dateutil import parser as dateparser
from dotenv import load_dotenv

# Load .env from the project root (one level above crawler/).
load_dotenv(Path(__file__).parent.parent / ".env")

HEADERS = {"User-Agent": "FeedbeatAgent/1.0 (open source news aggregator)"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Remove UTM parameters and tracking suffixes."""
    tracked = {"utm_source", "utm_medium", "utm_campaign",
               "utm_content", "utm_term", "ref", "source", "fbclid"}
    p = urlparse(url)
    params = {k: v for k, v in parse_qs(p.query).items()
              if k.lower() not in tracked}
    return p._replace(query=urlencode(params, doseq=True), fragment="").geturl()


def extract_urls(text: str) -> list[str]:
    """Extract all http(s) URLs from text."""
    pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    return [normalize_url(u) for u in re.findall(pattern, text)]


def strip_html(text: str) -> str:
    """Simple HTML stripping without an external library."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def extract_hashtags(text: str) -> list[str]:
    """Extract hashtags from text (deduplicated, lowercase)."""
    return list(dict.fromkeys(t.lower() for t in re.findall(r'#(\w{2,})', text)))


def bluesky_post_url(post_uri: str, handle: str) -> str:
    """Convert an at:// post URI to its public bsky.app URL."""
    if not post_uri.startswith("at://") or not handle:
        return ""
    parts = post_uri.split("/")
    if len(parts) < 5:
        return ""
    return f"https://bsky.app/profile/{handle}/post/{parts[-1]}"


async def fetch_title(url: str, client: httpx.AsyncClient) -> str:
    """Fetch the <title> tag from a URL. Returns an empty string on failure."""
    try:
        r = await client.get(url, timeout=8, follow_redirects=True)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
        if m:
            return strip_html(m.group(1)).strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Mastodon
# ---------------------------------------------------------------------------

async def collect_mastodon(domain: str, instances: list[str]) -> list[dict]:
    """
    Authenticated Mastodon search for a domain.
    Token and instance are loaded from .env (MASTODON_TOKEN, MASTODON_INSTANCE).
    The own instance is searched first; other instances are used as fallbacks.
    """
    token    = os.getenv("MASTODON_TOKEN", "")
    own_inst = os.getenv("MASTODON_INSTANCE", "").rstrip("/")

    headers = {**HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Eigene Instanz zuerst, dann weitere
    search_instances = []
    if own_inst:
        search_instances.append(own_inst)
    search_instances += [i for i in instances if i.rstrip("/") != own_inst]

    signals = []
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        for instance in search_instances:
            try:
                r = await client.get(
                    f"{instance}/api/v2/search",
                    params={"q": domain, "type": "statuses", "limit": 40},
                )
                if r.status_code == 401 or r.status_code == 422:
                    # Missing auth on this instance — fall back to tag timeline.
                    tag = domain.split(".")[0]
                    r = await client.get(
                        f"{instance}/api/v1/timelines/tag/{tag}",
                        params={"limit": 40},
                    )
                if r.status_code != 200:
                    print(f"  Mastodon {instance}: HTTP {r.status_code}")
                    continue
                for status in r.json() if isinstance(r.json(), list) else r.json().get("statuses", []):
                    content = strip_html(status.get("content", ""))
                    urls = extract_urls(content)
                    acc = status.get("account", {})
                    tags = extract_hashtags(content)
                    for url in urls:
                        if domain in url:
                            signals.append({
                                "url":            url,
                                "title":          "",
                                "platform":       "mastodon",
                                "curator_handle": acc.get("acct", ""),
                                "curator_meta": {
                                    "followers":  acc.get("followers_count", 0),
                                    "following":  acc.get("following_count", 0),
                                    "posts":      acc.get("statuses_count", 0),
                                    "created_at": acc.get("created_at", ""),
                                },
                                "shared_at": status.get("created_at", ""),
                                "syndication_url": status.get("url", ""),
                                "engagement": {
                                    "boosts":  status.get("reblogs_count", 0),
                                    "likes":   status.get("favourites_count", 0),
                                    "replies": status.get("replies_count", 0),
                                    "quotes":  0,
                                },
                                "tags":       tags,
                            })
            except Exception as e:
                print(f"  Mastodon {instance}: {e}")
    return signals


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

async def _bluesky_token() -> str:
    """Log in once with the app password and return an access token."""
    handle   = os.getenv("BLUESKY_HANDLE", "")
    password = os.getenv("BLUESKY_APP_PASSWORD", "")
    if not handle or not password:
        return ""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
            r = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": handle, "password": password},
            )
            if r.status_code == 200:
                return r.json().get("accessJwt", "")
            print(f"  Bluesky login failed: HTTP {r.status_code}")
    except Exception as e:
        print(f"  Bluesky login: {e}")
    return ""


async def collect_bluesky(domain: str, limit: int = 25) -> list[dict]:
    """Authenticated Bluesky search. Token is loaded from .env."""
    token = await _bluesky_token()
    headers = {**HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    signals = []
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        try:
            r = await client.get(
                "https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                params={"q": domain, "limit": limit},
            )
            if r.status_code != 200:
                return signals
            for post in r.json().get("posts", []):
                record  = post.get("record", {})
                author  = post.get("author", {})
                facets  = record.get("facets", [])
                text    = record.get("text", "")

                # URLs and tags from facets.
                urls = []
                tags = []
                for facet in facets:
                    for feat in facet.get("features", []):
                        ftype = feat.get("$type", "")
                        if ftype == "app.bsky.richtext.facet#link":
                            uri = feat.get("uri", "")
                            if uri.startswith("http"):
                                urls.append(normalize_url(uri))
                        elif ftype == "app.bsky.richtext.facet#tag":
                            tags.append(feat.get("tag", "").lower())
                # Fallback: regex over text.
                if not urls:
                    urls = extract_urls(text)
                if not tags:
                    tags = extract_hashtags(text)

                followers = author.get("followersCount", 0)
                following = author.get("followsCount", 0)
                posts_cnt = author.get("postsCount", 0)
                author_handle = author.get("handle", "")
                syndication_url = bluesky_post_url(post.get("uri", ""), author_handle)

                # Bluesky search often does not return follower counts.
                # Fallback so the curator filter does not drop everything.
                if followers == 0 and posts_cnt == 0:
                    followers = 100
                    posts_cnt = 50

                for url in urls:
                    if domain in url:
                        signals.append({
                            "url":            url,
                            "title":          "",
                            "platform":       "bluesky",
                            "curator_handle": author_handle,
                            "curator_meta": {
                                "followers":  followers,
                                "following":  following,
                                "posts":      posts_cnt,
                                "created_at": author.get("createdAt", ""),
                            },
                            "shared_at": record.get("createdAt", ""),
                            "syndication_url": syndication_url,
                            "engagement": {
                                "boosts":  post.get("repostCount", 0),
                                "likes":   post.get("likeCount", 0),
                                "replies": post.get("replyCount", 0),
                                "quotes":  post.get("quoteCount", 0),
                            },
                            "tags":       tags,
                        })
        except Exception as e:
            print(f"  Bluesky {domain}: {type(e).__name__}: {e}")
    return signals


# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------

async def collect_hackernews(top_n: int = 100, min_score: int = 10) -> list[dict]:
    """Hacker News top stories — fully public."""
    signals = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
        try:
            ids_r = await client.get(
                "https://hacker-news.firebaseio.com/v0/topstories.json"
            )
            item_ids = ids_r.json()[:top_n]
        except Exception as e:
            print(f"  HN topstories: {e}")
            return signals

        for item_id in item_ids:
            try:
                r = await client.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
                )
                item = r.json()
                if not item or item.get("score", 0) < min_score:
                    continue
                if not item.get("url"):
                    continue
                signals.append({
                    "url":            normalize_url(item["url"]),
                    "title":          item.get("title", ""),
                    "platform":       "hackernews",
                    "curator_handle": "hackernews",
                    "curator_meta":   {
                        "followers":  1000,
                        "following":  0,
                        "posts":      9999,
                        "created_at": "",
                    },
                    "hn_score":  item["score"],
                    "engagement": {
                        "boosts":  0,
                        "likes":   item.get("score", 0),
                        "replies": item.get("descendants", 0),
                        "quotes":  0,
                    },
                    "shared_at": datetime.fromtimestamp(
                        item.get("time", 0), tz=timezone.utc
                    ).isoformat(),
                })
            except Exception:
                pass
    return signals


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def collect_rss(feed_urls: list[str]) -> list[dict]:
    """Parse direct RSS/Atom feeds."""
    signals = []
    for url in feed_urls:
        try:
            feed = feedparser.parse(url)
            feed_title = feed.feed.get("title", url)
            for entry in feed.entries[:20]:
                link = getattr(entry, "link", "")
                if not link:
                    continue
                rss_tags = list(dict.fromkeys(
                    re.sub(r'\s+', '_', (t.get('term', '') or t.get('label', '')).strip().lower())
                    for t in getattr(entry, 'tags', [])
                    if (t.get('term') or t.get('label', '')).strip()
                ))
                signals.append({
                    "url":            normalize_url(link),
                    "title":          entry.get("title", ""),
                    "platform":       "rss",
                    "curator_handle": feed_title,
                    "feed_url":        url,
                    "curator_meta":   {
                        "followers":  1000,
                        "following":  0,
                        "posts":      9999,
                        "created_at": "",
                    },
                    "shared_at": entry.get(
                        "published",
                        datetime.now(timezone.utc).isoformat()
                    ),
                    "engagement": {
                        "boosts":  0,
                        "likes":   0,
                        "replies": 0,
                        "quotes":  0,
                    },
                    "tags": rss_tags,
                })
        except Exception as e:
            print(f"  RSS {url}: {e}")
    return signals


# ---------------------------------------------------------------------------
# Fetch missing titles
# ---------------------------------------------------------------------------

async def enrich_titles(signals: list[dict]) -> list[dict]:
    """Fill missing titles by fetching the article URL."""
    missing = [s for s in signals if not s.get("title")]
    if not missing:
        return signals

    # Deduplicate URLs.
    url_to_title: dict[str, str] = {}
    urls_needed = list({s["url"] for s in missing})

    async with httpx.AsyncClient(headers=HEADERS, timeout=8) as client:
        for url in urls_needed:
            title = await fetch_title(url, client)
            if title:
                url_to_title[url] = title

    for s in signals:
        if not s.get("title") and s["url"] in url_to_title:
            s["title"] = url_to_title[s["url"]]

    return signals
