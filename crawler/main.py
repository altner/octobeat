"""
main.py — OctoBeat bot entrypoint.
Combines all modules: discover -> filter -> score -> store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone

import yaml
from dateutil import parser as dateparser

from collector import collect_mastodon, collect_bluesky, collect_hackernews, collect_rss, enrich_titles, _bluesky_token, collect_mastodon_trends
from curator  import is_valid_curator, calc_weight
from database import (
    apply_curator_learning, apply_feed_learning, inactive_feed_urls, record_run,
    record_tag_history, record_unmapped_tags, load_corrections_from_db,
    export_computed, restore_from_computed,
)
try:
    from embedder import build_anchor_embeddings, classify_articles as embed_classify
    _EMBEDDER_AVAILABLE = True
except ImportError:
    _EMBEDDER_AVAILABLE = False
from scorer   import score_article
from storage  import write_feed, push_to_github


CRAWLER_DIR = Path(__file__).parent


def load_config() -> dict:
    cfg_path = CRAWLER_DIR / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    corrections_path = CRAWLER_DIR / "corrections.yaml"
    if corrections_path.exists():
        with open(corrections_path) as f:
            cfg["corrections"] = yaml.safe_load(f) or {}
    else:
        cfg["corrections"] = {}

    return cfg


def resolve_config_path(path: str) -> Path:
    configured = Path(path)
    if configured.is_absolute():
        return configured
    return (CRAWLER_DIR / configured).resolve()


def data_dir_from_config(cfg: dict) -> Path:
    return resolve_config_path(cfg.get("output", {}).get("data_dir", "../data"))


def feeds_path_from_config(cfg: dict) -> Path:
    return data_dir_from_config(cfg) / "feeds.json"


def load_feed_urls(cfg: dict) -> list[str]:
    """Load the source feed list. data/feeds.json is the source of truth."""
    feeds_path = feeds_path_from_config(cfg)
    if feeds_path.exists():
        try:
            data = json.loads(feeds_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(url).strip() for url in data if str(url).strip()]
            if isinstance(data, dict):
                return [str(url).strip() for url in data.get("feeds", []) if str(url).strip()]
        except Exception as exc:
            print(f"  Could not read {feeds_path}: {exc}")

    return [str(url).strip() for url in cfg.get("rss_feeds", []) if str(url).strip()]


def write_feed_urls(cfg: dict, feed_urls: list[str]) -> Path:
    feeds_path = feeds_path_from_config(cfg)
    feeds_path.parent.mkdir(parents=True, exist_ok=True)
    feeds_path.write_text(json.dumps(feed_urls, ensure_ascii=False, indent=2), encoding="utf-8")
    return feeds_path


def domains_from_feed_urls(feed_urls: list[str]) -> list[str]:
    """Derive social search domains from the configured RSS feed URLs."""
    domains: list[str] = []
    seen: set[str] = set()
    for feed_url in feed_urls:
        try:
            host = urlparse(feed_url).netloc.lower()
        except Exception:
            continue
        for prefix in ("www.", "rss.", "feeds."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        if not host or host in seen:
            continue
        seen.add(host)
        domains.append(host)
    return domains


def domain_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    for prefix in ("www.", "rss.", "feeds."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


def merge_domains(primary_domains: list[str], extra_domains: list[str]) -> list[str]:
    """Merge domains while keeping primary source domains first."""
    merged = []
    seen = set()
    for domain in [*primary_domains, *extra_domains]:
        if not domain or domain in seen:
            continue
        seen.add(domain)
        merged.append(domain)
    return merged


def domains_from_rss_signals(
    signals: list[dict],
    blacklist: set[str],
    max_domains: int,
) -> list[str]:
    """Derive extra social search domains from RSS article URLs."""
    counts: Counter[str] = Counter()
    for signal in signals:
        if signal.get("platform") != "rss":
            continue
        domain = domain_from_url(signal.get("url", ""))
        if domain and domain not in blacklist:
            counts[domain] += 1
    return [domain for domain, _ in counts.most_common(max_domains)]


def domain_ok(url: str, blacklist: set) -> bool:
    try:
        domain = domain_from_url(url)
        return bool(domain) and domain not in blacklist
    except Exception:
        return False


def newest_signal_time(signals: list[dict]) -> tuple[str, datetime | None]:
    """Return freshest shared_at value and parsed UTC datetime, if parseable."""
    newest_raw = ""
    newest_at: datetime | None = None

    for signal in signals:
        raw = signal.get("shared_at", "")
        if raw > newest_raw:
            newest_raw = raw

        try:
            parsed = dateparser.parse(raw)
        except Exception:
            continue

        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)

        if newest_at is None or parsed > newest_at:
            newest_at = parsed
            newest_raw = raw

    return newest_raw, newest_at


def normalize_tag(tag: str) -> str:
    """Lowercase tags and keep them stable for section ids."""
    tag = tag.strip().lower()
    tag = re.sub(r"\s+", "-", tag)
    return re.sub(r"[^a-z0-9äöüß_-]", "", tag)


def infer_article_tags(
    title: str, url: str, signals: list[dict], tag_rules: dict, tag_map: dict,
    embed_scores: list[tuple[str, float]] | None = None,
) -> tuple[list[str], list[dict]]:
    """Build section tags from three sources, highest to lowest weight:
    1. tag_map: source tags from RSS/social directly mapped to a category  (+3)
    2. tag_rules: keyword match on title+URL                               (+1)
    3. raw source tags that survive as-is when no mapping exists           (+2, kept separate)

    Returns (tags, debug_entries) where debug_entries explain each assignment.
    """
    category_counts: Counter[str] = Counter()
    raw_tag_counts: Counter[str] = Counter()
    debug: list[dict] = []

    # Build reverse lookup: source_tag → category
    source_to_category: dict[str, str] = {}
    for category, source_tags in tag_map.items():
        cat_norm = normalize_tag(category)
        if not cat_norm:
            continue
        for st in source_tags:
            source_to_category[str(st).casefold().replace(" ", "_")] = cat_norm

    _noise = re.compile(r"^(uberblogr|uberbot|blogrhaus\d+|linktipp)$")

    for signal in signals:
        for tag in signal.get("tags", []):
            tag_norm = normalize_tag(tag)
            if not tag_norm or _noise.match(tag_norm):
                continue
            category = source_to_category.get(tag_norm)
            if category:
                category_counts[category] += 3
                debug.append({"category": category, "source": "tag_map", "via": tag_norm, "weight": 3})
            else:
                raw_tag_counts[tag_norm] += 2
                debug.append({"category": tag_norm, "source": "raw_tag", "via": tag_norm, "weight": 2})

    text = f"{title} {url}".casefold()
    for tag, keywords in tag_rules.items():
        cat_norm = normalize_tag(tag)
        if not cat_norm:
            continue
        for keyword in keywords:
            kw = str(keyword).casefold()
            pattern = rf"(?<![a-zäöüß]){re.escape(kw)}"
            if re.search(pattern, text):
                category_counts[cat_norm] += 1
                debug.append({"category": cat_norm, "source": "keyword", "via": kw, "weight": 1})
                break

    # Embedding-based scores (+2..+4 depending on similarity, weight scale from embedder)
    if embed_scores:
        from embedder import WEIGHT_SCALE, SIMILARITY_THRESHOLD
        for cat, sim in embed_scores:
            cat_norm = normalize_tag(cat)
            if not cat_norm:
                continue
            weight = round(WEIGHT_SCALE * sim)
            category_counts[cat_norm] += weight
            debug.append({"category": cat_norm, "source": "embedding",
                          "via": f"sim={sim:.2f}", "weight": weight})

    combined: Counter[str] = category_counts + raw_tag_counts
    tags = [tag for tag, _ in combined.most_common(5)]
    return tags, debug


def collect_syndications(signals: list[dict]) -> list[dict]:
    """Return unique social post links that syndicated an article."""
    seen: set[str] = set()
    syndications = []

    for signal in signals:
        url = signal.get("syndication_url", "")
        if not url or url in seen:
            continue

        seen.add(url)
        syndications.append({
            "platform": signal.get("platform", ""),
            "curator": signal.get("curator_handle", ""),
            "url": url,
            "shared_at": signal.get("shared_at", ""),
        })

    syndications.sort(key=lambda item: item.get("shared_at", ""), reverse=True)
    return syndications


def aggregate_engagement(signals: list[dict]) -> dict:
    """Aggregate engagement counts across all signals for transparency."""
    totals = {"boosts": 0, "likes": 0, "replies": 0, "quotes": 0}
    for signal in signals:
        engagement = signal.get("engagement", {})
        for key in totals:
            totals[key] += int(engagement.get(key, 0) or 0)
    return totals


async def run():
    run_started_at = datetime.now(timezone.utc)
    cfg       = load_config()
    blacklist = set(cfg.get("domain_blacklist", []))
    rss_urls = load_feed_urls(cfg)
    feed_domains = domains_from_feed_urls(rss_urls)
    all_signals: list[dict] = []
    rss_signals: list[dict] = []
    print(f"→ Loaded {len(rss_urls)} feeds and derived {len(feed_domains)} source domains")

    # ── 1. RSS ─────────────────────────────────────────────────────────────
    if rss_urls:
        print("→ Crawling RSS feeds...")
        rss_signals = collect_rss(rss_urls)
        all_signals.extend(rss_signals)
        print(f"  {len(rss_signals)} signals from {len(rss_urls)} feeds")

    social_cfg = cfg.get("social_search", {})
    rss_article_domains = domains_from_rss_signals(
        rss_signals,
        blacklist,
        social_cfg.get("max_rss_article_domains", 50),
    )
    source_domains = merge_domains(feed_domains, rss_article_domains)
    if rss_article_domains:
        print(f"→ Added {len(source_domains) - len(feed_domains)} RSS article domains for social search")

    # ── 2. Mastodon Trending Links ─────────────────────────────────────────
    print("→ Fetching Mastodon trending links...")
    trend_signals = await collect_mastodon_trends(cfg.get("mastodon_instances", []))
    all_signals.extend(trend_signals)

    # ── 3. Mastodon domain search ──────────────────────────────────────────
    print("→ Crawling Mastodon...")
    mastodon_results = await asyncio.gather(
        *[collect_mastodon(domain, cfg.get("mastodon_instances", [])) for domain in source_domains]
    )
    for domain, sigs in zip(source_domains, mastodon_results):
        all_signals.extend(sigs)
        print(f"  {domain}: {len(sigs)} signals")

    # ── 4. Bluesky ─────────────────────────────────────────────────────────
    bsky_cfg = cfg.get("bluesky", {})
    if bsky_cfg.get("enabled", True):
        print("→ Crawling Bluesky...")
        bsky_token = await _bluesky_token()
        bluesky_results = await asyncio.gather(
            *[collect_bluesky(domain, bsky_cfg.get("limit", 25), token=bsky_token) for domain in source_domains]
        )
        for domain, sigs in zip(source_domains, bluesky_results):
            all_signals.extend(sigs)
            print(f"  {domain}: {len(sigs)} signals")

    # ── 5. Hacker News ─────────────────────────────────────────────────────
    hn_cfg = cfg.get("hackernews", {})
    if hn_cfg.get("enabled", True):
        print("→ Crawling Hacker News...")
        sigs = await collect_hackernews(hn_cfg.get("top_n", 100), hn_cfg.get("min_score", 10))
        all_signals.extend(sigs)
        print(f"  HN: {len(sigs)} signals")

    # ── 6. Filter: domain blacklist + curator validation ──────────────────
    valid_signals = []
    for s in all_signals:
        if not domain_ok(s["url"], blacklist):
            continue
        if not is_valid_curator(s["curator_meta"], cfg):
            continue
        s["weight"] = calc_weight(s["curator_meta"], s["platform"])
        valid_signals.append(s)

    print(f"→ {len(valid_signals)} valid signals (of {len(all_signals)})")

    learning_cfg = cfg.get("learning", {})
    learning_db_path = resolve_config_path(learning_cfg.get("db_path", "../data/octobeat.sqlite3"))
    data_dir = data_dir_from_config(cfg)
    computed_path = Path(data_dir) / "computed.json"

    # Auto-restore from computed.json if DB is missing or empty
    if not learning_db_path.exists():
        print("⚠ SQLite DB not found — attempting restore from computed.json...")
        restored = restore_from_computed(learning_db_path, computed_path)
        if not restored:
            print("  No computed.json found — starting fresh.")

    if learning_cfg.get("enabled", True):
        learned = apply_curator_learning(
            valid_signals,
            learning_db_path,
            learning_cfg.get("min_curator_signals", 3),
            learning_cfg.get("max_curator_bonus", 0.25),
        )
        if learned:
            print(f"→ Applied learning bonus to {learned} signals")
        feed_learned = apply_feed_learning(valid_signals, learning_db_path)
        if feed_learned:
            print(f"→ Applied feed ratings to {feed_learned} RSS signals")

    # ── 5. Fetch missing titles where needed ───────────────────────────────
    print("→ Fetching missing titles...")
    valid_signals = await enrich_titles(valid_signals)

    # ── 6. Group signals by URL ────────────────────────────────────────────
    by_url: dict[str, list] = {}
    for s in valid_signals:
        by_url.setdefault(s["url"], []).append(s)

    # ── 7. Build and score articles ────────────────────────────────────────
    # Pre-compute embedding classifications if available
    embed_cfg = cfg.get("embeddings", {})
    embed_results: dict[str, list[tuple[str, float]]] = {}
    if _EMBEDDER_AVAILABLE and embed_cfg.get("enabled", True):
        print("→ Computing semantic embeddings...")
        anchor_embeddings = build_anchor_embeddings(cfg.get("tag_anchors", {}))
        if anchor_embeddings:
            proto_articles = [
                {"url": url, "title": next((s.get("title") for s in sigs if s.get("title")), "")}
                for url, sigs in by_url.items()
            ]
            embed_results = embed_classify(proto_articles, anchor_embeddings, learning_db_path)
            print(f"  {len(embed_results)} articles classified via embeddings")

    articles = []
    max_age_h = cfg["article_filter"].get("max_age_hours", 48)
    now = datetime.now(timezone.utc)
    corrections = {**cfg.get("corrections", {}), **load_corrections_from_db(learning_db_path)}
    too_old_count = 0

    for url, sigs in by_url.items():
        # The newest shared_at decides whether the article may stay in the feed.
        latest_shared, latest_at = newest_signal_time(sigs)
        if latest_at is not None:
            age_hours = (now - latest_at).total_seconds() / 3600
            if age_hours > max_age_h:
                too_old_count += 1
                continue

        score = score_article(sigs)
        title = next((s.get("title") for s in sigs if s.get("title")), "")

        # Derive tags: keyword rules + tag_map + embeddings
        article_tags, tag_debug = infer_article_tags(
            title, url, sigs, cfg.get("tag_rules", {}), cfg.get("tag_map", {}),
            embed_scores=embed_results.get(url),
        )

        # Apply manual corrections (YAML file + DB, DB takes precedence).
        corrected = corrections.get(url)
        if corrected is not None:
            article_tags = [normalize_tag(t) for t in corrected if normalize_tag(t)]
            tag_debug = [{"category": t, "source": "correction", "via": "corrections.yaml", "weight": 99}
                         for t in article_tags]

        articles.append({
            "id":            hashlib.md5(url.encode()).hexdigest()[:12],
            "url":           url,
            "title":         title,
            "score":         score,
            "signal_count":  len(sigs),
            "curators":      list(set(s["curator_handle"] for s in sigs)),
            "platforms":     list(set(s["platform"]       for s in sigs)),
            "latest_shared": latest_shared,
            "syndications":  collect_syndications(sigs),
            "engagement":    aggregate_engagement(sigs),
            "tags":          article_tags,
            "tag_debug":     tag_debug,
        })

    if too_old_count:
        print(f"→ Dropped {too_old_count} finds older than {max_age_h}h")

    # ── 8. Keep only articles with social signal + meaningful title ────────
    social_platforms = {"mastodon", "bluesky"}
    _numeric_title = re.compile(r"^\d+$")
    articles = [
        a for a in articles
        if social_platforms & set(a["platforms"])
        and not _numeric_title.match((a.get("title") or "").strip())
    ]
    print(f"→ {len(articles)} finds with social signal (Mastodon/Bluesky)")

    # ── 9. Sort and keep top N ─────────────────────────────────────────────
    articles.sort(key=lambda a: a["score"], reverse=True)
    top_n = cfg["article_filter"]["top_n"]
    top   = articles[:top_n]

    print(f"→ Selected top {len(top)} finds (from {len(articles)} unique URLs)")

    if learning_cfg.get("enabled", True):
        run_id = record_run(learning_db_path, run_started_at, top, valid_signals, rss_urls)
        print(f"✓ Stored run #{run_id} in SQLite → {learning_db_path}")
        record_tag_history(learning_db_path, run_id, top)
        record_unmapped_tags(learning_db_path, top)

        pruning_cfg = learning_cfg.get("source_pruning", {})
        if pruning_cfg.get("enabled", True):
            inactive_feeds = inactive_feed_urls(
                learning_db_path,
                rss_urls,
                pruning_cfg.get("min_runs", 5),
                pruning_cfg.get("min_age_days", 14),
                pruning_cfg.get("require_no_social_signals", True),
            )
            if inactive_feeds:
                print(f"→ Flagged {len(inactive_feeds)} inactive feeds")
                for item in inactive_feeds:
                    print(
                        "  "
                        f"{item['domain']}: {item['runs_seen']} runs, "
                        f"{item['social_signal_sum']} social signals"
                    )
                if pruning_cfg.get("auto_remove", False):
                    inactive_set = {item["feed_url"] for item in inactive_feeds}
                    rss_urls = [url for url in rss_urls if url not in inactive_set]
                    print(f"→ Removed {len(inactive_feeds)} inactive feeds from feeds.json")

    # ── 9. Curator overview ────────────────────────────────────────────────
    for platform in ("mastodon", "bluesky"):
        counts = Counter(
            s["curator_handle"] for s in valid_signals if s["platform"] == platform
        )
        if counts:
            print(f"\n→ Top curators ({platform.capitalize()}):")
            for handle, count in counts.most_common(10):
                print(f"  {handle:<40} shared {count} links")
        else:
            print(f"\n→ No {platform.capitalize()} curators found.")

    # ── 10. Write and push ─────────────────────────────────────────────────
    out = cfg["output"]
    data_dir = data_dir_from_config(cfg)
    write_feed(top, data_dir)

    # Export learned data to computed.json (DB backup / restore source)
    if learning_cfg.get("enabled", True):
        export_computed(learning_db_path, Path(data_dir))

    # feeds.json for the settings page
    feeds_path = write_feed_urls(cfg, rss_urls)
    print(f"✓ {len(rss_urls)} feeds → {feeds_path}")
    if out.get("github_push", False):
        push_to_github(str(data_dir), out["commit_message"])


if __name__ == "__main__":
    asyncio.run(run())
