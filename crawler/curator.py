"""
curator.py — Kuratorgewicht automatisch aus öffentlichen Account-Daten ableiten.
Kein manuelles Eingreifen nötig.
"""

import math
from datetime import datetime, timezone
from dateutil import parser as dateparser


def is_valid_curator(meta: dict, cfg: dict) -> bool:
    """Spam- und Bot-Filter."""
    f = cfg["curator_filter"]
    if meta.get("followers", 0) < f["min_followers"]:
        return False
    if meta.get("following", 0) > f["max_following"]:
        return False
    if meta.get("posts", 0) < f["min_posts"]:
        return False
    return True


def calc_weight(meta: dict, platform: str) -> float:
    """
    Gewicht aus öffentlichen Account-Daten berechnen.

    Faktoren:
    - Follower-Anzahl (logarithmisch skaliert)
    - Follower/Following-Verhältnis
    - Account-Alter
    - Plattform-Basis

    Rückgabe: float im Bereich [0.1, 3.0]
    """
    weight = 1.0

    # Follower: logarithmisch
    # 100 Follower  → +0.0
    # 1.000         → +0.20
    # 10.000        → +0.40
    # 100.000       → +0.60
    followers = meta.get("followers", 0)
    if followers > 100:
        weight += math.log10(followers / 100) * 0.2

    # Follower/Following-Verhältnis
    following = max(meta.get("following", 1), 1)
    ratio = followers / following
    if ratio > 2:
        weight *= 1.2   # mehr Follower als Following → gutes Signal
    elif ratio < 0.1:
        weight *= 0.4   # folgt viel mehr als Follower → schwaches Signal

    # Account-Alter
    created = meta.get("created_at")
    if created:
        try:
            age_days = (
                datetime.now(timezone.utc)
                - dateparser.parse(created).astimezone(timezone.utc)
            ).days
            if age_days < 30:
                weight *= 0.3   # sehr neuer Account
            elif age_days < 180:
                weight *= 0.7   # junger Account
        except Exception:
            pass

    # Plattform-Basis
    platform_base = {
        "mastodon":   1.0,
        "bluesky":    1.0,
        "hackernews": 1.5,  # HN-Community = hohe redaktionelle Qualität
        "rss":        0.8,  # RSS ist kein soziales Signal
    }
    weight *= platform_base.get(platform, 1.0)

    return round(max(0.1, min(3.0, weight)), 3)
