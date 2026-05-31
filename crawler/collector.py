"""
collector.py — collect link signals from Mastodon, Bluesky, Hacker News, and RSS.
Each signal = {url, platform, curator_handle, curator_meta, shared_at, title?}
"""

import os
import re
import asyncio
import httpx
import feedparser
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs
from datetime import datetime, timezone
from dateutil import parser as dateparser
from dotenv import load_dotenv

# Load .env from the project root (one level above crawler/).
load_dotenv(Path(__file__).parent.parent / ".env")

HEADERS = {"User-Agent": "OctoBeatBot/1.0 (open source link discovery crawler)"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL_TRAILING_JUNK = re.compile(
    r'(via|per|from|durch|quelle|source|ref|cc|ht|h/t)$', re.IGNORECASE
)


def normalize_url(url: str) -> str:
    """Remove UTM parameters, tracking suffixes, AMP/Jetpack params, and trailing junk."""
    # Strip trailing punctuation and common appended words (e.g. "htmlvia")
    url = re.sub(r'[.,;:!?)]+$', '', url)
    # Split off trailing non-path words glued without separator (e.g. "htmlvia" → "html")
    url = re.sub(r'(\.html?|\.php|\.aspx?)(via|per|from|durch|quelle|cc|ht).*$',
                 r'\1', url, flags=re.IGNORECASE)
    # Decode encoded semicolons used by WordPress Jetpack (?amp%3Butm_medium=jetpack_social)
    url = url.replace('%3B', '&').replace('%3b', '&')
    tracked = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
               "utm_term", "ref", "source", "fbclid", "amp",
               "like_comment", "_wpnonce", "replytocom"}
    p = urlparse(url)
    params = {k: v for k, v in parse_qs(p.query).items()
              if k.lower() not in tracked}
    return p._replace(query=urlencode(params, doseq=True), fragment="").geturl()


def extract_urls(text: str) -> list[str]:
    """Extract all http(s) URLs from text."""
    pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    return [normalize_url(u) for u in re.findall(pattern, text)]


def strip_html(text: str) -> str:
    """Strip HTML tags and decode HTML entities."""
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


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


def _title_from_slug(url: str) -> str:
    """Derive a readable title from the URL slug as last resort."""
    from urllib.parse import unquote
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        return ""
    # Remove leading date patterns like 2026-05-31-
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", slug)
    return unquote(slug).replace("-", " ").replace("_", " ").strip().capitalize()


_USELESS_TITLES = re.compile(
    r"^(status|blog|home|index|untitled|feed|news|artikel|post)$", re.IGNORECASE
)


async def fetch_title(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Fetch title and og:description from a URL in a single request.

    Returns (title, description).  title falls back to URL slug; description
    may be empty if neither og:description nor meta[name=description] is found.
    """
    description = ""
    try:
        r = await client.get(url, timeout=8, follow_redirects=True)
        text = r.text

        # ── Title ─────────────────────────────────────────────────────────
        title = ""
        m_og = re.search(
            r'(?:property=["\']og:title["\'][^>]*content|content[^>]*property=["\']og:title["\'])'
            r'[^>]*=["\']([^"\']{4,})',
            text, re.IGNORECASE,
        )
        if m_og:
            t = strip_html(m_og.group(1)).strip()
            if t and not _USELESS_TITLES.match(t):
                title = t
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
            if m:
                t = strip_html(m.group(1)).strip()
                if t and not _USELESS_TITLES.match(t):
                    title = t
        if not title:
            title = _title_from_slug(url)

        # ── Description ───────────────────────────────────────────────────
        # og:description first, then meta[name=description]
        m_desc = re.search(
            r'(?:property=["\']og:description["\'][^>]*content|content[^>]*property=["\']og:description["\'])'
            r'[^>]*=["\']([^"\']{10,})',
            text, re.IGNORECASE,
        )
        if not m_desc:
            m_desc = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{10,})',
                text, re.IGNORECASE,
            )
        if m_desc:
            description = strip_html(m_desc.group(1)).strip()[:300]

        return title, description
    except Exception:
        pass
    return _title_from_slug(url), ""


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
                print(f"  Mastodon {instance}: {type(e).__name__}: {e}")
    return signals


# ---------------------------------------------------------------------------
# Mastodon Trending Links
# ---------------------------------------------------------------------------

async def collect_mastodon_trends(instances: list[str]) -> list[dict]:
    """
    Fetch trending article links from Mastodon instances via /api/v1/trends/links.
    Returns signals with platform='mastodon_trend' and curator_handle=instance domain.
    The 'accounts' count from the API is used as a synthetic signal multiplier.
    """
    token    = os.getenv("MASTODON_TOKEN", "")
    own_inst = os.getenv("MASTODON_INSTANCE", "").rstrip("/")

    headers = {**HEADERS}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    all_instances = []
    if own_inst:
        all_instances.append(own_inst)
    all_instances += [i for i in instances if i.rstrip("/") != own_inst]

    signals = []
    now = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        results = await asyncio.gather(
            *[_fetch_trends(inst, client, now) for inst in all_instances],
            return_exceptions=True,
        )

    for inst, result in zip(all_instances, results):
        if isinstance(result, Exception):
            print(f"  Trends {inst}: {result}")
            continue
        signals.extend(result)

    # Deduplicate by URL — keep entry with highest accounts count
    best: dict[str, dict] = {}
    for s in signals:
        url = s["url"]
        existing = best.get(url)
        if not existing or s["curator_meta"]["followers"] > existing["curator_meta"]["followers"]:
            best[url] = s

    print(f"  {len(best)} unique trending links from {len(all_instances)} instances")
    return list(best.values())


async def _fetch_trends(instance: str, client: httpx.AsyncClient, now: str) -> list[dict]:
    signals = []
    try:
        r = await client.get(f"{instance}/api/v1/trends/links", params={"limit": 20})
        if r.status_code != 200:
            print(f"  Trends {instance}: HTTP {r.status_code}")
            return signals
        for item in r.json():
            url = normalize_url(item.get("url", ""))
            if not url:
                continue
            title   = item.get("title") or item.get("description") or ""
            accounts = int(item.get("accounts", 1))
            uses     = int(item.get("uses", 1))
            inst_domain = instance.replace("https://", "").replace("http://", "")
            signals.append({
                "url":            url,
                "title":          strip_html(title).strip(),
                "platform":       "mastodon",
                "curator_handle": f"trends@{inst_domain}",
                "curator_meta": {
                    # Use accounts count as synthetic follower proxy for weight calc
                    "followers":  accounts * 100,
                    "following":  1,
                    "posts":      uses,
                    "created_at": "2020-01-01T00:00:00Z",
                },
                "shared_at":       now,
                "syndication_url": f"{instance}/explore",
                "engagement": {
                    "boosts":  uses,
                    "likes":   accounts,
                    "replies": 0,
                    "quotes":  0,
                },
                "tags": [],
            })
    except Exception as e:
        print(f"  Trends {instance}: {e}")
    return signals


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

_bluesky_token_cache = None  # type: str | None


async def _bluesky_token() -> str:
    """Log in once with the app password and return an access token. Cached per process."""
    global _bluesky_token_cache
    if _bluesky_token_cache is not None:
        return _bluesky_token_cache
    handle   = os.getenv("BLUESKY_HANDLE", "")
    password = os.getenv("BLUESKY_APP_PASSWORD", "")
    if not handle or not password:
        _bluesky_token_cache = ""
        return ""
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
            r = await client.post(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={"identifier": handle, "password": password},
            )
            if r.status_code == 200:
                _bluesky_token_cache = r.json().get("accessJwt", "")
                return _bluesky_token_cache
            print(f"  Bluesky login failed: HTTP {r.status_code}")
    except Exception as e:
        print(f"  Bluesky login: {e}")
    _bluesky_token_cache = ""
    return ""


async def collect_bluesky(domain: str, limit: int = 25, token: str = "") -> list[dict]:
    """Authenticated Bluesky search. Token is loaded from .env."""
    if not token:
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
                # feedparser provides entry.summary (HTML) for most feeds
                raw_summary = getattr(entry, "summary", "") or ""
                summary = strip_html(raw_summary).strip()[:300] if raw_summary else ""
                signals.append({
                    "url":            normalize_url(link),
                    "title":          entry.get("title", ""),
                    "description":    summary,
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
    """Fill missing titles (and extract og:description) by fetching article URLs."""
    missing = [s for s in signals if not s.get("title")]
    if not missing:
        return signals

    # Deduplicate URLs.
    url_to_meta: dict[str, tuple[str, str]] = {}  # url → (title, description)
    urls_needed = list({s["url"] for s in missing})

    sem = asyncio.Semaphore(20)

    async def _fetch(url: str, client: httpx.AsyncClient) -> tuple[str, str, str]:
        async with sem:
            title, desc = await fetch_title(url, client)
            return url, title, desc

    async with httpx.AsyncClient(headers=HEADERS, timeout=8) as client:
        results = await asyncio.gather(*[_fetch(u, client) for u in urls_needed])
    for url, title, desc in results:
        if title:
            url_to_meta[url] = (title, desc)

    for s in signals:
        if not s.get("title") and s["url"] in url_to_meta:
            title, desc = url_to_meta[s["url"]]
            s["title"] = title
            if desc:
                s["description"] = desc

    return signals


# ---------------------------------------------------------------------------
# Article content enrichment (for LLM classification)
# ---------------------------------------------------------------------------

# Block-level tags whose content is boilerplate — strip entire subtrees
_BOILERPLATE_TAGS = re.compile(
    r"<(script|style|nav|header|footer|aside|form|noscript|button|figure|figcaption)"
    r"[\s>].*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Paragraph and heading tags — mark them so we can split on them later
_BLOCK_TAGS = re.compile(r"</?(?:p|h[1-6]|li|blockquote|article|section)[^>]*>", re.IGNORECASE)


def _extract_body_text(html: str, max_chars: int = 500) -> str:
    """Extract the first ~500 chars of readable body text from raw HTML.

    Strategy:
    1. Strip boilerplate blocks (nav, footer, scripts …)
    2. Split on block-level tags to get paragraph-like chunks
    3. Strip remaining tags, normalise whitespace
    4. Skip chunks that look like navigation/metadata (very short or all-caps)
    5. Return the first meaningful paragraphs up to max_chars
    """
    # Remove boilerplate subtrees first
    cleaned = _BOILERPLATE_TAGS.sub(" ", html)
    # Split into chunks on block boundaries
    chunks = _BLOCK_TAGS.split(cleaned)
    _metrics_noise = re.compile(
        r'\b(kommentare|retweets?|likes?|shares?|views?|follower)\s*:?\s*\d+',
        re.IGNORECASE,
    )
    result_parts: list[str] = []
    total = 0
    for chunk in chunks:
        text = strip_html(chunk)
        text = re.sub(r"\s+", " ", text).strip()
        text = _metrics_noise.sub("", text).strip()
        # Skip navigation-like noise: too short, or suspiciously many caps/special chars
        if len(text) < 40:
            continue
        if sum(1 for c in text if c.isupper()) / max(len(text), 1) > 0.5:
            continue
        result_parts.append(text)
        total += len(text)
        if total >= max_chars:
            break
    combined = " ".join(result_parts)
    return combined[:max_chars].rsplit(" ", 1)[0] if len(combined) > max_chars else combined


async def enrich_article_content(
    articles: list[dict],
    max_concurrent: int = 8,
) -> list[dict]:
    """Fetch and extract body text for each article (for LLM context).

    Only fetches articles that don't already have a `content` field.
    Adds `content` (up to 500 chars of readable body text) to each article dict.
    """
    needed = [a for a in articles if not a.get("content")]
    if not needed:
        return articles

    url_to_content: dict[str, str] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _fetch(a: dict, client: httpx.AsyncClient) -> None:
        async with sem:
            try:
                r = await client.get(a["url"], timeout=10, follow_redirects=True)
                text = _extract_body_text(r.text)
                if text:
                    url_to_content[a["url"]] = text
                    # Also backfill description if missing
                    if not a.get("description"):
                        m = re.search(
                            r'(?:property=["\']og:description["\'][^>]*content|'
                            r'content[^>]*property=["\']og:description["\'])'
                            r'[^>]*=["\']([^"\']{10,})',
                            r.text, re.IGNORECASE,
                        )
                        if not m:
                            m = re.search(
                                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{10,})',
                                r.text, re.IGNORECASE,
                            )
                        if m:
                            a["description"] = strip_html(m.group(1)).strip()[:300]
            except Exception:
                pass

    async with httpx.AsyncClient(headers=HEADERS, timeout=10) as client:
        await asyncio.gather(*[_fetch(a, client) for a in needed])

    for a in articles:
        if a["url"] in url_to_content:
            a["content"] = url_to_content[a["url"]]

    return articles


# ---------------------------------------------------------------------------
# WordPress REST API engagement enrichment
# ---------------------------------------------------------------------------

def _wp_slug_from_url(url: str) -> str:
    """Extract the post slug from a WordPress URL path."""
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else ""


async def _fetch_wp_engagement(
    url: str, client: httpx.AsyncClient
) -> tuple[str, dict]:
    """
    Try to fetch comment count (and likes if available) from the WP REST API.
    Returns (url, {comments, likes}) or (url, {}) on failure.
    """
    parsed = urlparse(url)
    base   = f"{parsed.scheme}://{parsed.netloc}"
    slug   = _wp_slug_from_url(url)
    if not slug:
        return url, {}
    try:
        r = await client.get(
            f"{base}/wp-json/wp/v2/posts",
            params={"slug": slug, "_fields": "id,comment_count,jetpack_likes_enabled"},
            timeout=6,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return url, {}
        posts = r.json()
        if not posts:
            return url, {}
        post = posts[0]
        result = {"comments": int(post.get("comment_count", 0) or 0)}

        # Try Jetpack likes if post ID available
        post_id = post.get("id")
        if post_id and post.get("jetpack_likes_enabled"):
            lr = await client.get(
                f"https://public-api.wordpress.com/rest/v1.1/sites/{parsed.netloc}/posts/{post_id}/likes",
                timeout=6,
            )
            if lr.status_code == 200:
                result["likes"] = int(lr.json().get("found", 0))

        return url, result
    except Exception:
        return url, {}


async def enrich_wordpress_engagement(
    articles: list[dict], db_path=None
) -> list[dict]:
    """
    Enrich articles from WordPress sites with comment/like counts via REST API.
    Results are cached in SQLite (wp_engagement table).
    """
    import sqlite3
    from datetime import datetime, timezone

    # Load cache from DB
    cache: dict[str, dict] = {}
    if db_path and Path(str(db_path)).exists():
        try:
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT url, data_json FROM wp_engagement").fetchall()
            con.close()
            for row in rows:
                import json as _json
                cache[row["url"]] = _json.loads(row["data_json"])
        except Exception:
            pass

    # Identify WordPress URLs not yet cached
    wp_articles = [
        a for a in articles
        if ("wordpress.com" in a["url"] or _wp_slug_from_url(a["url"]))
        and a["url"] not in cache
    ]

    if wp_articles:
        sem = asyncio.Semaphore(10)

        async def _fetch(a: dict, client: httpx.AsyncClient):
            async with sem:
                return await _fetch_wp_engagement(a["url"], client)

        async with httpx.AsyncClient(headers=HEADERS) as client:
            results = await asyncio.gather(*[_fetch(a, client) for a in wp_articles])

        # Save to cache and DB
        import json as _json
        now = datetime.now(timezone.utc).isoformat()
        if db_path:
            try:
                con = sqlite3.connect(str(db_path))
                con.execute("PRAGMA journal_mode=DELETE")
                for url, data in results:
                    if data:
                        cache[url] = data
                        con.execute(
                            """INSERT OR REPLACE INTO wp_engagement(url, data_json, fetched_at)
                               VALUES (?, ?, ?)""",
                            (url, _json.dumps(data), now),
                        )
                con.commit()
                con.close()
            except Exception:
                pass
        else:
            for url, data in results:
                if data:
                    cache[url] = data

    # Apply to articles
    enriched = 0
    for a in articles:
        wp_data = cache.get(a["url"], {})
        if wp_data:
            a["wp_engagement"] = wp_data
            enriched += 1

    if enriched:
        print(f"  {enriched} articles enriched with WordPress engagement data")

    return articles
