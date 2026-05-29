"""
scorer.py — calculate article scores.
Score = sum(curator weights) × diversity bonus × platform bonus × engagement bonus × time decay
"""

import math
from datetime import datetime, timezone
from dateutil import parser as dateparser


def score_article(signals: list[dict]) -> float:
    """
    signals: list of dicts with keys: weight, platform, curator_handle, shared_at

    Formula:
        score = raw_weight × diversity × platform_bonus × engagement_bonus × time_factor × 1000

    - raw_weight: sum of all curator weights
    - diversity: log2(unique_curators + 1), rewarding independent curators
    - platform_bonus: small bonus when an article appears on several platforms
    - engagement_bonus: logarithmic bonus for boosts/reposts, likes, quotes, replies
    - time_factor: 1 / (age_hours + 2)^1.4, exponential aging
    """
    if not signals:
        return 0.0

    # 1. Sum of all curator weights.
    raw_weight = sum(s.get("weight", 1.0) for s in signals)

    # 2. Diversity bonus: different curators count more.
    unique_curators = len(set(s["curator_handle"] for s in signals))
    diversity = math.log2(unique_curators + 1)

    # 3. Platform diversity.
    unique_platforms = len(set(s["platform"] for s in signals))
    platform_bonus = 1 + (unique_platforms - 1) * 0.15

    # 4. Engagement: visible, but not dominant.
    engagement_points = 0.0
    for s in signals:
        engagement = s.get("engagement", {})
        engagement_points += engagement.get("boosts", 0) * 1.0
        engagement_points += engagement.get("quotes", 0) * 0.8
        engagement_points += engagement.get("replies", 0) * 0.5
        engagement_points += engagement.get("likes", 0) * 0.25

    engagement_bonus = 1 + min(math.log1p(engagement_points) / 10, 0.5)

    # 5. Time decay: the freshest signal determines article age.
    times = []
    for s in signals:
        try:
            t = dateparser.parse(s["shared_at"]).astimezone(timezone.utc)
            times.append(t)
        except Exception:
            pass

    age_hours = 24.0  # Fallback
    if times:
        newest = max(times)
        age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        age_hours = max(age_hours, 0.1)

    time_factor = 1.0 / math.pow(age_hours + 2, 1.4)

    score = raw_weight * diversity * platform_bonus * engagement_bonus * time_factor * 1000
    return round(score, 2)
