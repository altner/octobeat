"""
storage.py — write feed.json and optionally push to GitHub.
In GitHub Actions, git-auto-commit-action handles the push.
"""

import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone


def write_feed(articles: list[dict], data_dir: str, top_curators=None) -> Path:
    """Write feed.json and the daily archive."""
    base = Path(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "archive").mkdir(exist_ok=True)

    now = datetime.now(timezone.utc)
    output = {
        "generated_at":  now.isoformat(),
        "article_count": len(articles),
        "top_curators":  top_curators or {},
        "articles":      articles,
    }
    payload = json.dumps(output, ensure_ascii=False, indent=2)

    feed_path = base / "feed.json"
    feed_path.write_text(payload, encoding="utf-8")

    archive_path = base / "archive" / f"{now.strftime('%Y-%m-%d')}.json"
    archive_path.write_text(payload, encoding="utf-8")

    print(f"✓ {len(articles)} finds → {feed_path}")
    return feed_path


def push_to_github(data_dir: str, message: str) -> bool:
    """
    Git commit and push inside data_dir.
    Automatically skipped when GITHUB_ACTIONS=true is set because the workflow
    handles the push via git-auto-commit-action.
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("ℹ GitHub Actions detected — workflow handles the push.")
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
                print("ℹ No changes to commit.")
                return True
            print(f"Git error ({' '.join(cmd[:3])}): {r.stderr.strip()}")
            return False

    print("✓ GitHub updated")
    return True
