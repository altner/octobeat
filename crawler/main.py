"""
main.py — Feedbeat Einstiegspunkt.
Führt alle Module zusammen: sammeln → filtern → bewerten → speichern.
"""

import asyncio
import hashlib
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, timezone

import yaml
from dateutil import parser as dateparser

from collector import collect_mastodon, collect_bluesky, collect_hackernews, collect_rss, enrich_titles
from curator  import is_valid_curator, calc_weight
from scorer   import score_article
from storage  import write_feed, push_to_github


def load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def domain_ok(url: str, blacklist: set) -> bool:
    try:
        domain = urlparse(url).netloc.replace("www.", "")
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


async def run():
    cfg       = load_config()
    blacklist = set(cfg.get("domain_blacklist", []))
    all_signals: list[dict] = []

    # ── 1. Mastodon ────────────────────────────────────────────────────────
    print("→ Mastodon crawlen...")
    for domain in cfg["seed_domains"]:
        sigs = await collect_mastodon(domain, cfg.get("mastodon_instances", []))
        all_signals.extend(sigs)
        print(f"  {domain}: {len(sigs)} Signale")

    # ── 2. Bluesky ─────────────────────────────────────────────────────────
    bsky_cfg = cfg.get("bluesky", {})
    if bsky_cfg.get("enabled", True):
        print("→ Bluesky crawlen...")
        for domain in cfg["seed_domains"]:
            sigs = await collect_bluesky(domain, bsky_cfg.get("limit", 25))
            all_signals.extend(sigs)
            print(f"  {domain}: {len(sigs)} Signale")

    # ── 3. RSS ─────────────────────────────────────────────────────────────
    rss_urls = cfg.get("rss_feeds", [])
    if rss_urls:
        print("→ RSS-Feeds crawlen...")
        sigs = collect_rss(rss_urls)
        all_signals.extend(sigs)
        print(f"  {len(sigs)} Signale aus {len(rss_urls)} Feeds")

    # ── 2. Hacker News ─────────────────────────────────────────────────────
    hn_cfg = cfg.get("hackernews", {})
    if hn_cfg.get("enabled", True):
        print("→ Hacker News crawlen...")
        sigs = await collect_hackernews(hn_cfg.get("top_n", 100), hn_cfg.get("min_score", 10))
        all_signals.extend(sigs)
        print(f"  HN: {len(sigs)} Signale")

    # ── 4. Filter: Domain-Blacklist + Kuratoren-Validierung ────────────────
    valid_signals = []
    for s in all_signals:
        if not domain_ok(s["url"], blacklist):
            continue
        if not is_valid_curator(s["curator_meta"], cfg):
            continue
        s["weight"] = calc_weight(s["curator_meta"], s["platform"])
        valid_signals.append(s)

    print(f"→ {len(valid_signals)} gültige Signale (von {len(all_signals)})")

    # ── 5. Titel nachladen wo nötig ────────────────────────────────────────
    print("→ Fehlende Titel nachladen...")
    valid_signals = await enrich_titles(valid_signals)

    # ── 6. Signale nach URL gruppieren ─────────────────────────────────────
    by_url: dict[str, list] = {}
    for s in valid_signals:
        by_url.setdefault(s["url"], []).append(s)

    # ── 7. Artikel bauen + scoren ──────────────────────────────────────────
    articles = []
    max_age_h = cfg["article_filter"].get("max_age_hours", 48)
    now = datetime.now(timezone.utc)
    too_old_count = 0

    for url, sigs in by_url.items():
        # Neuestes shared_at bestimmt, ob ein Artikel noch ins Feed darf.
        latest_shared, latest_at = newest_signal_time(sigs)
        if latest_at is not None:
            age_hours = (now - latest_at).total_seconds() / 3600
            if age_hours > max_age_h:
                too_old_count += 1
                continue

        score = score_article(sigs)
        title = next((s.get("title") for s in sigs if s.get("title")), "")

        # Tags aus Signalen aggregieren; Domain-Fallback für ungetaggte Artikel
        all_tags: list[str] = []
        for s in sigs:
            all_tags.extend(s.get("tags", []))
        article_tags = [t for t, _ in Counter(all_tags).most_common(5)]
        if not article_tags:
            domain = urlparse(url).netloc.replace("www.", "")
            article_tags = cfg.get("domain_tags", {}).get(domain, [])

        articles.append({
            "id":            hashlib.md5(url.encode()).hexdigest()[:12],
            "url":           url,
            "title":         title,
            "score":         score,
            "signal_count":  len(sigs),
            "curators":      list(set(s["curator_handle"] for s in sigs)),
            "platforms":     list(set(s["platform"]       for s in sigs)),
            "latest_shared": latest_shared,
            "tags":          article_tags,
        })

    if too_old_count:
        print(f"→ {too_old_count} Artikel älter als {max_age_h}h verworfen")

    # ── 8. Nur Artikel mit sozialem Signal behalten ────────────────────────
    social_platforms = {"mastodon", "bluesky"}
    articles = [
        a for a in articles
        if social_platforms & set(a["platforms"])
    ]
    print(f"→ {len(articles)} Artikel mit sozialem Signal (Mastodon/Bluesky)")

    # ── 9. Sortieren + Top N ───────────────────────────────────────────────
    articles.sort(key=lambda a: a["score"], reverse=True)
    top_n = cfg["article_filter"]["top_n"]
    top   = articles[:top_n]

    print(f"→ Top {len(top)} Artikel selektiert (von {len(articles)} unique URLs)")

    # ── 9. Kuratoren-Übersicht ─────────────────────────────────────────────
    for platform in ("mastodon", "bluesky"):
        counts = Counter(
            s["curator_handle"] for s in valid_signals if s["platform"] == platform
        )
        if counts:
            print(f"\n→ Top Kuratoren ({platform.capitalize()}):")
            for handle, count in counts.most_common(10):
                print(f"  {handle:<40} {count} Artikel geteilt")
        else:
            print(f"\n→ Keine {platform.capitalize()}-Kuratoren gefunden.")

    # ── 10. Schreiben + pushen ─────────────────────────────────────────────
    out = cfg["output"]
    write_feed(top, out["data_dir"])

    # feeds.json für die Unterseite
    import json as _json
    from pathlib import Path as _Path
    feeds_path = _Path(out["data_dir"]) / "feeds.json"
    feeds_path.write_text(
        _json.dumps(cfg.get("rss_feeds", []), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"✓ {len(cfg.get('rss_feeds', []))} Feeds → {feeds_path}")
    if out.get("github_push", False):
        push_to_github(out["data_dir"], out["commit_message"])


if __name__ == "__main__":
    asyncio.run(run())
