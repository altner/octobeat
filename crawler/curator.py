"""
curator.py — derive curator weight automatically from public account data.
No manual intervention required.
"""

import math
from datetime import datetime, timezone
from dateutil import parser as dateparser


def is_valid_curator(meta: dict, cfg: dict) -> bool:
    """Spam and bot filter."""
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
    Calculate weight from public account data.

    Factors:
    - follower count (logarithmic scale)
    - follower/following ratio
    - account age
    - platform baseline

    Returns a float in the range [0.1, 3.0].
    """
    weight = 1.0

    # Followers: logarithmic.
    # 100 Follower  → +0.0
    # 1.000         → +0.20
    # 10.000        → +0.40
    # 100.000       → +0.60
    followers = meta.get("followers", 0)
    if followers > 100:
        weight += math.log10(followers / 100) * 0.2

    # Follower/following ratio.
    following = max(meta.get("following", 1), 1)
    ratio = followers / following
    if ratio > 2:
        weight *= 1.2   # more followers than following -> good signal
    elif ratio < 0.1:
        weight *= 0.4   # follows far more accounts than followers -> weak signal

    # Account age.
    created = meta.get("created_at")
    if created:
        try:
            age_days = (
                datetime.now(timezone.utc)
                - dateparser.parse(created).astimezone(timezone.utc)
            ).days
            if age_days < 30:
                weight *= 0.3   # very new account
            elif age_days < 180:
                weight *= 0.7   # young account
        except Exception:
            pass

    # Platform baseline.
    platform_base = {
        "mastodon":   1.0,
        "bluesky":    1.0,
        "hackernews": 1.5,  # HN community = high editorial quality
        "rss":        0.8,  # RSS is not a social signal
    }
    weight *= platform_base.get(platform, 1.0)

    return round(max(0.1, min(3.0, weight)), 3)
