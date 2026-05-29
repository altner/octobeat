"""
database.py — persistent memory for the crawler.
SQLite stays local and small; the static frontend still reads feed.json.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1


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
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


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


def record_run(
    db_path: Path,
    started_at: datetime,
    articles: list[dict],
    signals: list[dict],
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

    return run_id
