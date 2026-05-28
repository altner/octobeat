"""
scorer.py — Artikel-Score berechnen.
Score = Σ(Kuratorgewichte) × Diversitätsbonus × Plattformbonus × Zeitverfall
"""

import math
from datetime import datetime, timezone
from dateutil import parser as dateparser


def score_article(signals: list[dict]) -> float:
    """
    signals: Liste von Dicts mit keys: weight, platform, curator_handle, shared_at

    Formel:
        score = raw_weight × diversity × platform_bonus × time_factor × 1000

    - raw_weight:    Summe aller Kuratorgewichte
    - diversity:     log2(unique_curators + 1) — Bonifikation wenn mehrere unabhängige
                     Kuratoren denselben Artikel teilen
    - platform_bonus: kleiner Bonus wenn Artikel auf mehreren Plattformen erscheint
    - time_factor:   1 / (age_hours + 2)^1.4 — exponentielle Alterung
    """
    if not signals:
        return 0.0

    # 1. Summe aller Kuratorgewichte
    raw_weight = sum(s.get("weight", 1.0) for s in signals)

    # 2. Diversitätsbonus: verschiedene Kuratoren zählen mehr
    unique_curators = len(set(s["curator_handle"] for s in signals))
    diversity = math.log2(unique_curators + 1)

    # 3. Plattform-Diversität
    unique_platforms = len(set(s["platform"] for s in signals))
    platform_bonus = 1 + (unique_platforms - 1) * 0.15

    # 4. Zeitverfall: frischestes Signal bestimmt Alter
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

    score = raw_weight * diversity * platform_bonus * time_factor * 1000
    return round(score, 2)
