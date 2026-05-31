"""
database.py — persistent memory for the crawler.
SQLite stays local and small; the static frontend still reads feed.json.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


SCHEMA_VERSION = 2


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(db_path: Path) -> None:
    with connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              finished_at TEXT NOT NULL,
              article_count INTEGER NOT NULL,
              signal_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS articles (
              url TEXT PRIMARY KEY,
              title TEXT,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              seen_count INTEGER NOT NULL DEFAULT 0,
              best_score REAL NOT NULL DEFAULT 0,
              latest_score REAL NOT NULL DEFAULT 0,
              latest_rank INTEGER
            );

            CREATE TABLE IF NOT EXISTS run_articles (
              run_id INTEGER NOT NULL,
              url TEXT NOT NULL,
              rank INTEGER NOT NULL,
              score REAL NOT NULL,
              title TEXT,
              platforms_json TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              engagement_json TEXT NOT NULL,
              PRIMARY KEY (run_id, url),
              FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id INTEGER NOT NULL,
              article_url TEXT NOT NULL,
              platform TEXT NOT NULL,
              curator_handle TEXT NOT NULL,
              shared_at TEXT,
              syndication_url TEXT,
              weight REAL NOT NULL DEFAULT 1,
              engagement_json TEXT NOT NULL,
              FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_signals_curator
              ON signals(platform, curator_handle);
            CREATE INDEX IF NOT EXISTS idx_signals_url
              ON signals(article_url);

            CREATE TABLE IF NOT EXISTS curator_stats (
              platform TEXT NOT NULL,
              curator_handle TEXT NOT NULL,
              total_signals INTEGER NOT NULL DEFAULT 0,
              top_articles INTEGER NOT NULL DEFAULT 0,
              score_sum REAL NOT NULL DEFAULT 0,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              PRIMARY KEY (platform, curator_handle)
            );

            CREATE TABLE IF NOT EXISTS curator_feedback (
              platform TEXT NOT NULL,
              curator_handle TEXT NOT NULL,
              rating INTEGER NOT NULL DEFAULT 0,
              blocked INTEGER NOT NULL DEFAULT 0,
              note TEXT,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (platform, curator_handle)
            );

            CREATE TABLE IF NOT EXISTS feed_feedback (
              feed_url TEXT PRIMARY KEY,
              rating INTEGER NOT NULL DEFAULT 0,
              blocked INTEGER NOT NULL DEFAULT 0,
              note TEXT,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_runs (
              run_id INTEGER NOT NULL,
              feed_url TEXT NOT NULL,
              domain TEXT NOT NULL,
              rss_signal_count INTEGER NOT NULL DEFAULT 0,
              social_signal_count INTEGER NOT NULL DEFAULT 0,
              top_article_count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (run_id, feed_url),
              FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            CREATE TABLE IF NOT EXISTS source_stats (
              feed_url TEXT PRIMARY KEY,
              domain TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              runs_seen INTEGER NOT NULL DEFAULT 0,
              rss_signal_sum INTEGER NOT NULL DEFAULT 0,
              social_signal_sum INTEGER NOT NULL DEFAULT 0,
              top_article_sum INTEGER NOT NULL DEFAULT 0,
              last_rss_signal_at TEXT,
              last_social_signal_at TEXT
            );

            -- Embedding-based classification (Stufe 2)
            CREATE TABLE IF NOT EXISTS article_embeddings (
              url TEXT PRIMARY KEY,
              model TEXT NOT NULL,
              embedding BLOB NOT NULL,
              updated_at TEXT NOT NULL
            );

            -- Tag assignment history for learning
            CREATE TABLE IF NOT EXISTS tag_history (
              url TEXT NOT NULL,
              run_id INTEGER NOT NULL,
              tags_json TEXT NOT NULL,
              tag_debug_json TEXT NOT NULL,
              PRIMARY KEY (url, run_id),
              FOREIGN KEY (run_id) REFERENCES runs(id)
            );

            -- Manual corrections (mirror of corrections.yaml, editable via /learn)
            CREATE TABLE IF NOT EXISTS tag_corrections (
              url TEXT PRIMARY KEY,
              tags_json TEXT NOT NULL,
              note TEXT,
              updated_at TEXT NOT NULL
            );

            -- Unmapped RSS tag log — feeds the /learn page suggestions
            CREATE TABLE IF NOT EXISTS unmapped_tags (
              tag TEXT NOT NULL,
              domain TEXT NOT NULL,
              count INTEGER NOT NULL DEFAULT 0,
              last_seen TEXT NOT NULL,
              PRIMARY KEY (tag, domain)
            );

            -- WordPress REST API engagement cache
            CREATE TABLE IF NOT EXISTS wp_engagement (
              url TEXT PRIMARY KEY,
              data_json TEXT NOT NULL,
              fetched_at TEXT NOT NULL
            );
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def domain_from_url(url: str) -> str:
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return ""
    for prefix in ("www.", "rss.", "feeds."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain


def curator_multipliers(
    db_path: Path,
    min_signals: int = 3,
    max_bonus: float = 0.25,
) -> dict[tuple[str, str], float]:
    """Return learned curator multipliers from previous runs."""
    init_db(db_path)
    multipliers: dict[tuple[str, str], float] = {}

    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT platform, curator_handle, total_signals, top_articles, score_sum
            FROM curator_stats
            WHERE total_signals >= ?
            """,
            (min_signals,),
        ).fetchall()
        feedback_rows = con.execute(
            """
            SELECT platform, curator_handle, rating, blocked
            FROM curator_feedback
            """
        ).fetchall()

    for row in rows:
        total = max(int(row["total_signals"]), 1)
        top = int(row["top_articles"])
        top_rate = top / total
        consistency = min(math.log1p(total) * 0.03, max_bonus / 2)
        quality = min(top_rate * 0.2, max_bonus / 2)
        multiplier = 1 + min(consistency + quality, max_bonus)
        multipliers[(row["platform"], row["curator_handle"])] = round(multiplier, 3)

    for row in feedback_rows:
        key = (row["platform"], row["curator_handle"])
        if int(row["blocked"]):
            multipliers[key] = 0.05
            continue

        rating = max(-5, min(5, int(row["rating"])))
        if rating == 0:
            continue

        current = multipliers.get(key, 1.0)
        manual_multiplier = 1 + rating * 0.15
        multipliers[key] = round(max(0.2, min(1.5, current * manual_multiplier)), 3)

    return multipliers


def apply_curator_learning(
    signals: list[dict],
    db_path: Path,
    min_signals: int = 3,
    max_bonus: float = 0.25,
) -> int:
    """Apply learned curator multipliers to signal weights."""
    multipliers = curator_multipliers(db_path, min_signals, max_bonus)
    applied = 0

    for signal in signals:
        key = (signal.get("platform", ""), signal.get("curator_handle", ""))
        multiplier = multipliers.get(key)
        if not multiplier:
            continue
        signal["learning_multiplier"] = multiplier
        signal["weight"] = round(signal.get("weight", 1.0) * multiplier, 3)
        applied += 1

    return applied


def feed_multipliers(db_path: Path) -> dict[str, float]:
    """Return manual RSS feed multipliers."""
    init_db(db_path)
    multipliers: dict[str, float] = {}

    with connect(db_path) as con:
        rows = con.execute(
            "SELECT feed_url, rating, blocked FROM feed_feedback"
        ).fetchall()

    for row in rows:
        feed_url = row["feed_url"]
        if int(row["blocked"]):
            multipliers[feed_url] = 0.05
            continue
        rating = max(-5, min(5, int(row["rating"])))
        if rating:
            multipliers[feed_url] = round(max(0.2, min(1.75, 1 + rating * 0.1)), 3)

    return multipliers


def feed_feedback_rows(db_path: Path) -> list[dict]:
    """Return manual RSS feed feedback rows for the local settings UI."""
    init_db(db_path)
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT feed_url, rating, blocked, updated_at
            FROM feed_feedback
            ORDER BY feed_url
            """
        ).fetchall()

    return [
        {
            "feed_url": row["feed_url"],
            "rating": int(row["rating"]),
            "blocked": bool(row["blocked"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def apply_feed_learning(signals: list[dict], db_path: Path) -> int:
    """Apply manual RSS feed feedback to RSS signal weights."""
    multipliers = feed_multipliers(db_path)
    applied = 0

    for signal in signals:
        if signal.get("platform") != "rss":
            continue
        multiplier = multipliers.get(signal.get("feed_url", ""))
        if not multiplier:
            continue
        signal["feed_multiplier"] = multiplier
        signal["weight"] = round(signal.get("weight", 1.0) * multiplier, 3)
        applied += 1

    return applied


def set_curator_feedback(
    db_path: Path,
    platform: str,
    curator_handle: str,
    rating: int | None = None,
    blocked: bool | None = None,
    note: str | None = None,
) -> dict:
    """Create or update manual curator feedback."""
    init_db(db_path)
    platform = platform.strip()
    curator_handle = curator_handle.strip()
    if not platform or not curator_handle:
        raise ValueError("platform and curator_handle are required")

    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        existing = con.execute(
            """
            SELECT rating, blocked, note
            FROM curator_feedback
            WHERE platform = ? AND curator_handle = ?
            """,
            (platform, curator_handle),
        ).fetchone()

        next_rating = int(existing["rating"]) if existing else 0
        next_blocked = bool(existing["blocked"]) if existing else False
        next_note = existing["note"] if existing else None

        if rating is not None:
            next_rating = max(-5, min(5, int(rating)))
        if blocked is not None:
            next_blocked = bool(blocked)
        if note is not None:
            next_note = note

        con.execute(
            """
            INSERT INTO curator_feedback(
              platform, curator_handle, rating, blocked, note, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, curator_handle) DO UPDATE SET
              rating=excluded.rating,
              blocked=excluded.blocked,
              note=excluded.note,
              updated_at=excluded.updated_at
            """,
            (
                platform,
                curator_handle,
                next_rating,
                1 if next_blocked else 0,
                next_note,
                now_iso,
            ),
        )

    return {
        "platform": platform,
        "curator_handle": curator_handle,
        "rating": next_rating,
        "blocked": next_blocked,
        "note": next_note,
        "updated_at": now_iso,
    }


def set_feed_feedback(
    db_path: Path,
    feed_url: str,
    rating: int | None = None,
    blocked: bool | None = None,
    note: str | None = None,
) -> dict:
    """Create or update manual RSS feed feedback."""
    init_db(db_path)
    feed_url = feed_url.strip()
    if not feed_url:
        raise ValueError("feed_url is required")

    now_iso = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        existing = con.execute(
            """
            SELECT rating, blocked, note
            FROM feed_feedback
            WHERE feed_url = ?
            """,
            (feed_url,),
        ).fetchone()

        next_rating = int(existing["rating"]) if existing else 0
        next_blocked = bool(existing["blocked"]) if existing else False
        next_note = existing["note"] if existing else None

        if rating is not None:
            next_rating = max(-5, min(5, int(rating)))
        if blocked is not None:
            next_blocked = bool(blocked)
        if note is not None:
            next_note = note

        con.execute(
            """
            INSERT INTO feed_feedback(feed_url, rating, blocked, note, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(feed_url) DO UPDATE SET
              rating=excluded.rating,
              blocked=excluded.blocked,
              note=excluded.note,
              updated_at=excluded.updated_at
            """,
            (feed_url, next_rating, 1 if next_blocked else 0, next_note, now_iso),
        )

    return {
        "feed_url": feed_url,
        "rating": next_rating,
        "blocked": next_blocked,
        "note": next_note,
        "updated_at": now_iso,
    }


def record_source_stats(
    con: sqlite3.Connection,
    run_id: int,
    feed_urls: list[str],
    signals: list[dict],
    top_urls: set[str],
    now_iso: str,
) -> None:
    """Persist per-source RSS and social signal counts for pruning decisions."""
    rss_counts: dict[str, int] = {}
    social_counts: dict[str, int] = {}
    top_counts: dict[str, int] = {}
    feed_article_domains: dict[str, set[str]] = {}

    for signal in signals:
        platform = signal.get("platform", "")
        if platform == "rss":
            feed_url = signal.get("feed_url", "")
            if feed_url:
                rss_counts[feed_url] = rss_counts.get(feed_url, 0) + 1
                article_domain = domain_from_url(signal.get("url", ""))
                if article_domain:
                    feed_article_domains.setdefault(feed_url, set()).add(article_domain)
            continue

        if platform in {"mastodon", "bluesky"}:
            domain = domain_from_url(signal.get("url", ""))
            if domain:
                social_counts[domain] = social_counts.get(domain, 0) + 1

    for url in top_urls:
        domain = domain_from_url(url)
        if domain:
            top_counts[domain] = top_counts.get(domain, 0) + 1

    for feed_url in feed_urls:
        domain = domain_from_url(feed_url)
        rss_count = rss_counts.get(feed_url, 0)
        source_domains = {domain, *feed_article_domains.get(feed_url, set())}
        social_count = sum(social_counts.get(source_domain, 0) for source_domain in source_domains)
        top_count = sum(top_counts.get(source_domain, 0) for source_domain in source_domains)
        last_rss_signal_at = now_iso if rss_count else None
        last_social_signal_at = now_iso if social_count else None

        con.execute(
            """
            INSERT INTO source_runs(
              run_id, feed_url, domain, rss_signal_count,
              social_signal_count, top_article_count
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, feed_url, domain, rss_count, social_count, top_count),
        )
        con.execute(
            """
            INSERT INTO source_stats(
              feed_url, domain, first_seen, last_seen, runs_seen,
              rss_signal_sum, social_signal_sum, top_article_sum,
              last_rss_signal_at, last_social_signal_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(feed_url) DO UPDATE SET
              domain=excluded.domain,
              last_seen=excluded.last_seen,
              runs_seen=source_stats.runs_seen + 1,
              rss_signal_sum=source_stats.rss_signal_sum + excluded.rss_signal_sum,
              social_signal_sum=source_stats.social_signal_sum + excluded.social_signal_sum,
              top_article_sum=source_stats.top_article_sum + excluded.top_article_sum,
              last_rss_signal_at=COALESCE(excluded.last_rss_signal_at, source_stats.last_rss_signal_at),
              last_social_signal_at=COALESCE(excluded.last_social_signal_at, source_stats.last_social_signal_at)
            """,
            (
                feed_url,
                domain,
                now_iso,
                now_iso,
                rss_count,
                social_count,
                top_count,
                last_rss_signal_at,
                last_social_signal_at,
            ),
        )


def inactive_feed_urls(
    db_path: Path,
    feed_urls: list[str],
    min_runs: int = 5,
    min_age_days: int = 14,
    require_no_social_signals: bool = True,
) -> list[dict]:
    """Return feeds eligible for pruning based on accumulated source stats."""
    init_db(db_path)
    if not feed_urls:
        return []

    now = datetime.now(timezone.utc)
    cutoff = (
        datetime.min.replace(tzinfo=timezone.utc)
        if min_age_days <= 0
        else now - timedelta(days=min_age_days)
    )
    inactive = []

    with connect(db_path) as con:
        stats_rows = con.execute(
            """
            SELECT feed_url, domain, first_seen
            FROM source_stats
            """
        ).fetchall()

        stats_by_feed = {row["feed_url"]: row for row in stats_rows}

        for feed_url in feed_urls:
            row = stats_by_feed.get(feed_url)
            if not row:
                continue

            first_seen = datetime.fromisoformat(row["first_seen"])
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
            age_days = (now - first_seen.astimezone(timezone.utc)).total_seconds() / 86400
            if age_days < min_age_days:
                continue

            recent = con.execute(
                """
                SELECT COUNT(*) AS runs_seen,
                       COALESCE(SUM(rss_signal_count), 0) AS rss_signal_sum,
                       COALESCE(SUM(social_signal_count), 0) AS social_signal_sum,
                       COALESCE(SUM(top_article_count), 0) AS top_article_sum
                FROM source_runs
                JOIN runs ON runs.id = source_runs.run_id
                WHERE source_runs.feed_url = ?
                  AND runs.finished_at >= ?
                """,
                (feed_url, cutoff.isoformat()),
            ).fetchone()

            runs_seen = int(recent["runs_seen"])
            social_sum = int(recent["social_signal_sum"])
            rss_sum = int(recent["rss_signal_sum"])

            if runs_seen < min_runs:
                continue
            if require_no_social_signals and social_sum > 0:
                continue

            inactive.append({
                "feed_url": feed_url,
                "domain": row["domain"],
                "runs_seen": runs_seen,
                "age_days": round(age_days, 1),
                "rss_signal_sum": rss_sum,
                "social_signal_sum": social_sum,
                "top_article_sum": int(recent["top_article_sum"]),
            })

    return inactive


def source_status_rows(
    db_path: Path,
    feed_urls: list[str],
    min_runs: int = 5,
    min_age_days: int = 14,
    require_no_social_signals: bool = True,
) -> list[dict]:
    """Return source learning status for the local settings UI."""
    init_db(db_path)
    inactive = {
        item["feed_url"]: item
        for item in inactive_feed_urls(
            db_path,
            feed_urls,
            min_runs,
            min_age_days,
            require_no_social_signals,
        )
    }

    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT feed_url, domain, first_seen, last_seen, runs_seen,
                   rss_signal_sum, social_signal_sum, top_article_sum,
                   last_rss_signal_at, last_social_signal_at
            FROM source_stats
            """
        ).fetchall()

    stats_by_feed = {row["feed_url"]: row for row in rows}
    now = datetime.now(timezone.utc)
    statuses = []

    for feed_url in feed_urls:
        row = stats_by_feed.get(feed_url)
        if not row:
            statuses.append({
                "feed_url": feed_url,
                "domain": domain_from_url(feed_url),
                "status": "new",
                "min_runs": min_runs,
                "min_age_days": min_age_days,
                "runs_seen": 0,
                "rss_signal_sum": 0,
                "social_signal_sum": 0,
                "top_article_sum": 0,
                "age_days": 0,
                "prune_candidate": False,
            })
            continue

        first_seen = datetime.fromisoformat(row["first_seen"])
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        age_days = (now - first_seen.astimezone(timezone.utc)).total_seconds() / 86400
        social_sum = int(row["social_signal_sum"])
        runs_seen = int(row["runs_seen"])
        prune_candidate = feed_url in inactive

        if prune_candidate:
            status = "prune_candidate"
        elif runs_seen < min_runs or age_days < min_age_days:
            status = "learning"
        elif social_sum == 0:
            status = "no_social_signals"
        else:
            status = "healthy"

        statuses.append({
            "feed_url": feed_url,
            "domain": row["domain"],
            "status": status,
            "min_runs": min_runs,
            "min_age_days": min_age_days,
            "runs_seen": runs_seen,
            "rss_signal_sum": int(row["rss_signal_sum"]),
            "social_signal_sum": social_sum,
            "top_article_sum": int(row["top_article_sum"]),
            "last_rss_signal_at": row["last_rss_signal_at"],
            "last_social_signal_at": row["last_social_signal_at"],
            "age_days": round(age_days, 1),
            "prune_candidate": prune_candidate,
        })

    return statuses


def record_run(
    db_path: Path,
    started_at: datetime,
    articles: list[dict],
    signals: list[dict],
    feed_urls: list[str] | None = None,
) -> int:
    """Persist the completed crawler run and update historical stats."""
    init_db(db_path)
    finished_at = datetime.now(timezone.utc)
    now_iso = finished_at.isoformat()
    article_scores = {article["url"]: article.get("score", 0.0) for article in articles}
    top_urls = set(article_scores)

    with connect(db_path) as con:
        cur = con.execute(
            """
            INSERT INTO runs(started_at, finished_at, article_count, signal_count)
            VALUES (?, ?, ?, ?)
            """,
            (started_at.isoformat(), now_iso, len(articles), len(signals)),
        )
        run_id = int(cur.lastrowid)

        for rank, article in enumerate(articles, start=1):
            con.execute(
                """
                INSERT INTO articles(
                  url, title, first_seen, last_seen, seen_count,
                  best_score, latest_score, latest_rank
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                  title=excluded.title,
                  last_seen=excluded.last_seen,
                  seen_count=articles.seen_count + 1,
                  best_score=max(articles.best_score, excluded.best_score),
                  latest_score=excluded.latest_score,
                  latest_rank=excluded.latest_rank
                """,
                (
                    article["url"],
                    article.get("title", ""),
                    now_iso,
                    now_iso,
                    article.get("score", 0.0),
                    article.get("score", 0.0),
                    rank,
                ),
            )
            con.execute(
                """
                INSERT INTO run_articles(
                  run_id, url, rank, score, title,
                  platforms_json, tags_json, engagement_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    article["url"],
                    rank,
                    article.get("score", 0.0),
                    article.get("title", ""),
                    json.dumps(article.get("platforms", []), ensure_ascii=False),
                    json.dumps(article.get("tags", []), ensure_ascii=False),
                    json.dumps(article.get("engagement", {}), ensure_ascii=False),
                ),
            )

        for signal in signals:
            url = signal.get("url", "")
            platform = signal.get("platform", "")
            curator = signal.get("curator_handle", "")
            engagement_json = json.dumps(signal.get("engagement", {}), ensure_ascii=False)
            is_top = 1 if url in top_urls else 0
            score = article_scores.get(url, 0.0)
            shared_at = signal.get("shared_at", "")

            con.execute(
                """
                INSERT INTO signals(
                  run_id, article_url, platform, curator_handle,
                  shared_at, syndication_url, weight, engagement_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    url,
                    platform,
                    curator,
                    shared_at,
                    signal.get("syndication_url", ""),
                    signal.get("weight", 1.0),
                    engagement_json,
                ),
            )

            if platform and curator:
                con.execute(
                    """
                    INSERT INTO curator_stats(
                      platform, curator_handle, total_signals, top_articles,
                      score_sum, first_seen, last_seen
                    )
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(platform, curator_handle) DO UPDATE SET
                      total_signals=curator_stats.total_signals + 1,
                      top_articles=curator_stats.top_articles + excluded.top_articles,
                      score_sum=curator_stats.score_sum + excluded.score_sum,
                      last_seen=excluded.last_seen
                    """,
                    (platform, curator, is_top, score, now_iso, now_iso),
                )

        if feed_urls is not None:
            record_source_stats(con, run_id, feed_urls, signals, top_urls, now_iso)

    return run_id


# ── Learning helpers ──────────────────────────────────────────────────────────

def record_tag_history(
    db_path: Path, run_id: int, articles: list[dict]
) -> None:
    """Store tag assignments + debug info for every article in this run."""
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        for a in articles:
            con.execute(
                """
                INSERT OR REPLACE INTO tag_history(url, run_id, tags_json, tag_debug_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    a["url"],
                    run_id,
                    json.dumps(a.get("tags", []), ensure_ascii=False),
                    json.dumps(a.get("tag_debug", []), ensure_ascii=False),
                ),
            )


def record_unmapped_tags(db_path: Path, articles: list[dict]) -> None:
    """Log raw_tag entries that had no tag_map match — used by /learn suggestions."""
    from urllib.parse import urlparse as _up
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        for a in articles:
            domain = _up(a["url"]).netloc
            for entry in a.get("tag_debug", []):
                if entry["source"] != "raw_tag":
                    continue
                con.execute(
                    """
                    INSERT INTO unmapped_tags(tag, domain, count, last_seen)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(tag, domain) DO UPDATE SET
                      count = count + 1,
                      last_seen = excluded.last_seen
                    """,
                    (entry["via"], domain, now),
                )


def load_corrections_from_db(db_path: Path) -> dict[str, list[str]]:
    """Return {url: [tags]} from tag_corrections table."""
    if not db_path.exists():
        return {}
    with connect(db_path) as con:
        rows = con.execute("SELECT url, tags_json FROM tag_corrections").fetchall()
    return {r["url"]: json.loads(r["tags_json"]) for r in rows}


def save_correction(db_path: Path, url: str, tags: list[str], note: str = "") -> None:
    """Upsert a manual correction into the DB."""
    now = datetime.now(timezone.utc).isoformat()
    with connect(db_path) as con:
        con.execute(
            """
            INSERT INTO tag_corrections(url, tags_json, note, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              tags_json = excluded.tags_json,
              note = excluded.note,
              updated_at = excluded.updated_at
            """,
            (url, json.dumps(tags, ensure_ascii=False), note, now),
        )


def export_feed_json(db_path: Path, data_dir: Path) -> Path:
    """
    Build feed.json from the latest run stored in SQLite.
    This is the only step that runs in GitHub Actions — everything else is local.
    """
    with connect(db_path) as con:
        run = con.execute(
            "SELECT id, finished_at, article_count FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not run:
            raise RuntimeError("No runs found in SQLite — run the crawler locally first.")

        rows = con.execute(
            """
            SELECT ra.url, ra.title, ra.rank, ra.score,
                   ra.platforms_json, ra.tags_json, ra.engagement_json,
                   th.tag_debug_json
            FROM run_articles ra
            LEFT JOIN tag_history th ON th.url = ra.url AND th.run_id = ra.run_id
            WHERE ra.run_id = ?
            ORDER BY ra.rank
            """,
            (run["id"],),
        ).fetchall()

        corrections = {
            r["url"]: json.loads(r["tags_json"])
            for r in con.execute("SELECT url, tags_json FROM tag_corrections").fetchall()
        }

    articles = []
    for r in rows:
        url = r["url"]
        tags = corrections.get(url) or json.loads(r["tags_json"] or "[]")
        articles.append({
            "id":           __import__("hashlib").md5(url.encode()).hexdigest()[:12],
            "url":          url,
            "title":        r["title"] or "",
            "score":        r["score"],
            "tags":         tags,
            "tag_debug":    json.loads(r["tag_debug_json"] or "[]"),
            "platforms":    json.loads(r["platforms_json"] or "[]"),
            "engagement":   json.loads(r["engagement_json"] or "{}"),
            "signal_count": 0,
            "curators":     [],
            "latest_shared": "",
            "syndications": [],
        })

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "archive").mkdir(exist_ok=True)

    output = {
        "generated_at":  run["finished_at"],
        "article_count": len(articles),
        "articles":      articles,
    }
    payload = json.dumps(output, ensure_ascii=False, indent=2)

    feed_path = data_dir / "feed.json"
    feed_path.write_text(payload, encoding="utf-8")

    archive_path = data_dir / "archive" / f"{run['finished_at'][:10]}.json"
    archive_path.write_text(payload, encoding="utf-8")

    print(f"✓ Exported {len(articles)} articles from SQLite run #{run['id']} → {feed_path}")
    return feed_path


# ── computed.json export / restore ───────────────────────────────────────────

def export_computed(db_path: Path, data_dir: Path) -> Path:
    """
    Export all learned/computed data to data/computed.json.
    This file is committed to git and used to restore the DB if it's lost.
    """
    if not db_path.exists():
        return data_dir / "computed.json"

    with connect(db_path) as con:
        curator_stats = [
            dict(r) for r in con.execute(
                "SELECT * FROM curator_stats ORDER BY platform, curator_handle"
            ).fetchall()
        ]
        curator_feedback = [
            dict(r) for r in con.execute(
                "SELECT * FROM curator_feedback ORDER BY platform, curator_handle"
            ).fetchall()
        ]
        feed_feedback = [
            dict(r) for r in con.execute(
                "SELECT * FROM feed_feedback ORDER BY feed_url"
            ).fetchall()
        ]
        source_stats = [
            dict(r) for r in con.execute(
                "SELECT * FROM source_stats ORDER BY feed_url"
            ).fetchall()
        ]
        tag_corrections = [
            dict(r) for r in con.execute(
                "SELECT * FROM tag_corrections ORDER BY url"
            ).fetchall()
        ]
        unmapped_tags = [
            dict(r) for r in con.execute(
                "SELECT * FROM unmapped_tags ORDER BY count DESC, tag"
            ).fetchall()
        ]
        runs_count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "exported_at":      now,
        "runs_count":       runs_count,
        "curator_stats":    curator_stats,
        "curator_feedback": curator_feedback,
        "feed_feedback":    feed_feedback,
        "source_stats":     source_stats,
        "tag_corrections":  tag_corrections,
        "unmapped_tags":    unmapped_tags,
    }

    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / "computed.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ Exported computed data ({runs_count} runs) → {out}")
    return out


def restore_from_computed(db_path: Path, computed_path: Path) -> bool:
    """
    Restore learned data from computed.json into a fresh DB.
    Called automatically when DB is missing but computed.json exists.
    """
    if not computed_path.exists():
        return False

    try:
        data = json.loads(computed_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠ Could not read computed.json: {e}")
        return False

    init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    with connect(db_path) as con:
        for r in data.get("curator_stats", []):
            con.execute(
                """INSERT OR REPLACE INTO curator_stats
                   (platform, curator_handle, total_signals, top_articles,
                    score_sum, first_seen, last_seen)
                   VALUES (:platform, :curator_handle, :total_signals, :top_articles,
                           :score_sum, :first_seen, :last_seen)""", r
            )
        for r in data.get("curator_feedback", []):
            con.execute(
                """INSERT OR REPLACE INTO curator_feedback
                   (platform, curator_handle, rating, blocked, note, updated_at)
                   VALUES (:platform, :curator_handle, :rating, :blocked, :note, :updated_at)""", r
            )
        for r in data.get("feed_feedback", []):
            con.execute(
                """INSERT OR REPLACE INTO feed_feedback
                   (feed_url, rating, blocked, note, updated_at)
                   VALUES (:feed_url, :rating, :blocked, :note, :updated_at)""", r
            )
        for r in data.get("source_stats", []):
            con.execute(
                """INSERT OR REPLACE INTO source_stats
                   (feed_url, domain, first_seen, last_seen, runs_seen,
                    rss_signal_sum, social_signal_sum, top_article_sum,
                    last_rss_signal_at, last_social_signal_at)
                   VALUES (:feed_url, :domain, :first_seen, :last_seen, :runs_seen,
                           :rss_signal_sum, :social_signal_sum, :top_article_sum,
                           :last_rss_signal_at, :last_social_signal_at)""", r
            )
        for r in data.get("tag_corrections", []):
            con.execute(
                """INSERT OR REPLACE INTO tag_corrections
                   (url, tags_json, note, updated_at)
                   VALUES (:url, :tags_json, :note, :updated_at)""", r
            )
        for r in data.get("unmapped_tags", []):
            con.execute(
                """INSERT OR REPLACE INTO unmapped_tags
                   (tag, domain, count, last_seen)
                   VALUES (:tag, :domain, :count, :last_seen)""", r
            )

    runs_count = data.get("runs_count", 0)
    print(f"✓ Restored computed data ({runs_count} previous runs) from {computed_path}")
    return True
