"""
export.py — generate feed.json from the local SQLite database.

Run this after the crawler to update the static site data:
  python crawler/export.py

In GitHub Actions this is NOT used — the committed feed.json is the source.
"""

import sys
from pathlib import Path

CRAWLER_DIR = Path(__file__).parent
sys.path.insert(0, str(CRAWLER_DIR))

import yaml
from database import export_feed_json


def main() -> None:
    cfg_path = CRAWLER_DIR / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    learning_cfg = cfg.get("learning", {})
    db_path = CRAWLER_DIR / "../data/octobeat.sqlite3"
    if "db_path" in learning_cfg:
        db_path = Path(learning_cfg["db_path"])
    db_path = db_path.resolve()

    data_dir = CRAWLER_DIR / "../data"
    data_dir = data_dir.resolve()

    export_feed_json(db_path, data_dir)


if __name__ == "__main__":
    main()
