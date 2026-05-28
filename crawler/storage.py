"""
storage.py — feed.json schreiben und optional zu GitHub pushen.
In GitHub Actions übernimmt git-auto-commit-action den Push.
"""

import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone


def write_feed(articles: list[dict], data_dir: str) -> Path:
    """feed.json + Tages-Archiv schreiben."""
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "archive").mkdir(exist_ok=True)

    now = datetime.now(timezone.utc)
    output = {
        "generated_at":  now.isoformat(),
        "article_count": len(articles),
        "articles":      articles,
    }
    payload = json.dumps(output, ensure_ascii=False, indent=2)

    feed_path = base / "feed.json"
    feed_path.write_text(payload, encoding="utf-8")

    archive_path = base / "archive" / f"{now.strftime('%Y-%m-%d')}.json"
    archive_path.write_text(payload, encoding="utf-8")

    print(f"✓ {len(articles)} Artikel → {feed_path}")
    return feed_path


def push_to_github(data_dir: str, message: str) -> bool:
    """
    Git-Commit und Push im data_dir.
    Wird automatisch übersprungen wenn GITHUB_ACTIONS=true gesetzt ist
    (dort erledigt git-auto-commit-action den Push).
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("ℹ GitHub Actions erkannt — Push wird von Workflow übernommen.")
        return True

    repo = str(Path(data_dir).resolve())
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg  = message.replace("{timestamp}", ts)

    cmds = [
        ["git", "-C", repo, "add", "."],
        ["git", "-C", repo, "commit", "-m", msg],
        ["git", "-C", repo, "push"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            if "nothing to commit" in r.stdout or "nothing to commit" in r.stderr:
                print("ℹ Keine Änderungen zu committen.")
                return True
            print(f"Git-Fehler ({' '.join(cmd[:3])}): {r.stderr.strip()}")
            return False

    print("✓ GitHub aktualisiert")
    return True
