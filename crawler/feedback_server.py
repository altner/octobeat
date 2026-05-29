"""
feedback_server.py — local write API for debug feedback.
Only intended for local development; GitHub Pages remains static/read-only.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

from database import feed_feedback_rows, set_curator_feedback, set_feed_feedback


CRAWLER_DIR = Path(__file__).parent
CONFIG_PATH = CRAWLER_DIR / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_config_path(path: str) -> Path:
    configured = Path(path)
    if configured.is_absolute():
        return configured
    return (CRAWLER_DIR / configured).resolve()


def feedback_db_path() -> Path:
    cfg = load_config()
    learning = cfg.get("learning", {})
    return resolve_config_path(learning.get("db_path", "../data/octobeat.sqlite3"))


def feeds_json_path(cfg: dict) -> Path:
    output = cfg.get("output", {})
    return resolve_config_path(output.get("data_dir", "../data")) / "feeds.json"


def write_feeds_json(feeds: list[str], cfg: dict) -> None:
    path = feeds_json_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(feeds, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_feed_url(feed_url: str) -> str:
    feed_url = feed_url.strip()
    parsed = urlparse(feed_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("feed_url must be an absolute http(s) URL")
    return feed_url


def rss_feed_block(lines: list[str]) -> tuple[int, int]:
    """Return line indexes for the rss_feeds block."""
    start = next(
        (index for index, line in enumerate(lines) if re.match(r"^rss_feeds:\s*$", line)),
        None,
    )
    if start is None:
        raise ValueError("rss_feeds block not found in config.yaml")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z_][\w-]*:\s*", lines[index]):
            end = index
            break
    return start, end


def insert_feed_in_config(feed_url: str) -> None:
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    start, end = rss_feed_block(lines)

    insert_at = end
    while insert_at > start + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    while insert_at > start + 1 and lines[insert_at - 1].lstrip().startswith("#"):
        insert_at -= 1

    spacer = [""] if insert_at < len(lines) and lines[insert_at].lstrip().startswith("#") else []
    next_lines = lines[:insert_at] + [f"  - {feed_url}"] + spacer + lines[insert_at:]
    CONFIG_PATH.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def delete_feed_from_config(feed_url: str) -> None:
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    start, end = rss_feed_block(lines)
    next_lines = []
    removed = False

    for index, line in enumerate(lines):
        if start < index < end:
            match = re.match(r"^(\s*-\s+)(\S+)(.*)$", line)
            if match and match.group(2) == feed_url:
                removed = True
                continue
        next_lines.append(line)

    if not removed:
        raise ValueError("feed_url not found")
    CONFIG_PATH.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def add_feed(feed_url: str) -> dict:
    cfg = load_config()
    feeds = list(cfg.get("rss_feeds", []))
    feed_url = normalize_feed_url(feed_url)
    if feed_url not in feeds:
        feeds.append(feed_url)
        insert_feed_in_config(feed_url)
        write_feeds_json(feeds, cfg)
    return {"feed_url": feed_url, "feeds": feeds, "count": len(feeds)}


def delete_feed(feed_url: str) -> dict:
    cfg = load_config()
    feeds = list(cfg.get("rss_feeds", []))
    feed_url = normalize_feed_url(feed_url)
    next_feeds = [url for url in feeds if url != feed_url]
    if len(next_feeds) == len(feeds):
        raise ValueError("feed_url not found")
    delete_feed_from_config(feed_url)
    write_feeds_json(next_feeds, cfg)
    return {"feed_url": feed_url, "feeds": next_feeds, "count": len(next_feeds)}


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "OctoBeatFeedback/1.0"

    def end_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        allowed_origins = {"http://localhost:4321", "http://127.0.0.1:4321"}
        self.send_header(
            "Access-Control-Allow-Origin",
            origin if origin in allowed_origins else "http://localhost:4321",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json({"ok": True, "db_path": str(feedback_db_path())})
            return
        if self.path == "/feedback/feeds":
            self.send_json({"ok": True, "feedback": feed_feedback_rows(feedback_db_path())})
            return

        self.send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path == "/feedback/curator":
            self.handle_curator_feedback()
            return
        if self.path == "/feedback/feed":
            self.handle_feed_feedback()
            return
        if self.path == "/feeds/add":
            self.handle_add_feed()
            return
        if self.path == "/feeds/delete":
            self.handle_delete_feed()
            return

        self.send_json({"ok": False, "error": "not found"}, status=404)

    def read_payload(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def handle_curator_feedback(self) -> None:
        try:
            payload = self.read_payload()
            result = set_curator_feedback(
                feedback_db_path(),
                platform=payload.get("platform", ""),
                curator_handle=payload.get("curator_handle", ""),
                rating=payload.get("rating"),
                blocked=payload.get("blocked"),
                note=payload.get("note"),
            )
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        self.send_json({"ok": True, "feedback": result})

    def handle_feed_feedback(self) -> None:
        try:
            payload = self.read_payload()
            result = set_feed_feedback(
                feedback_db_path(),
                feed_url=payload.get("feed_url", ""),
                rating=payload.get("rating"),
                blocked=payload.get("blocked"),
                note=payload.get("note"),
            )
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        self.send_json({"ok": True, "feedback": result})

    def handle_add_feed(self) -> None:
        try:
            payload = self.read_payload()
            result = add_feed(payload.get("feed_url", ""))
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        self.send_json({"ok": True, **result})

    def handle_delete_feed(self) -> None:
        try:
            payload = self.read_payload()
            result = delete_feed(payload.get("feed_url", ""))
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return

        self.send_json({"ok": True, **result})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    host = "127.0.0.1"
    port = 8765
    print(f"OctoBeat feedback API → http://{host}:{port}")
    print(f"SQLite → {feedback_db_path()}")
    ThreadingHTTPServer((host, port), FeedbackHandler).serve_forever()


if __name__ == "__main__":
    main()
