"""
llm_client.py — launches the local MLX LLM classifier in an isolated subprocess.

mlx-lm uses Metal; loading it in the crawler's main process (which has already
initialized the macOS Network framework via httpx) and then forking crashes on
macOS 26 (nw_settings_child_has_forked, SIGSEGV). We therefore launch the worker
via os.posix_spawn — which never forks — exactly like embedder.py does.

The model (~5 GB) is downloaded on first use to ~/.cache/huggingface/hub/.
mlx-lm is an optional dependency (see requirements-ml.txt); if it is missing the
worker exits non-zero and we degrade gracefully.
"""

from __future__ import annotations

from pathlib import Path


def classify_articles_llm_subprocess(
    articles: list[dict],
    categories: dict[str, str],
    model_id: str,
    batch_size: int = 10,
    max_tokens: int = 512,
    timeout_s: int = 900,
) -> dict[str, list[str]]:
    """Classify articles into categories using a local MLX LLM.

    Args:
        articles:   [{"url", "title"}, ...] — typically just the final feed.
        categories: {category_name: short_hint} — the allowed label set.
        model_id:   Hugging Face MLX model id.
    Returns:
        {url: [category, ...]} for articles the model could classify.
    """
    import sys
    import json as _json
    import tempfile
    import time
    import signal
    import os as _os

    if not articles or not categories:
        return {}

    worker = Path(__file__).parent / "llm_worker.py"

    tmp_in  = tempfile.NamedTemporaryFile(mode="w", suffix="_llm_in.json",  delete=False)
    tmp_out = tempfile.NamedTemporaryFile(mode="w", suffix="_llm_out.json", delete=False)
    tmp_err = tempfile.NamedTemporaryFile(mode="w", suffix="_llm_err.log",  delete=False)
    try:
        _json.dump({
            "articles":   articles,
            "categories": categories,
            "model_id":   model_id,
            "batch_size": batch_size,
            "max_tokens": max_tokens,
        }, tmp_in)
        tmp_in.flush()
        tmp_in.close()
        tmp_out.close()
        tmp_err.close()

        errfd = _os.open(tmp_err.name, _os.O_WRONLY)
        try:
            pid = _os.posix_spawn(
                sys.executable,
                [sys.executable, str(worker), tmp_in.name, tmp_out.name],
                _os.environ,
                file_actions=[(_os.POSIX_SPAWN_DUP2, errfd, 2)],
            )
        finally:
            _os.close(errfd)

        deadline = time.monotonic() + timeout_s
        rc: int | None = None
        while time.monotonic() < deadline:
            wpid, status = _os.waitpid(pid, _os.WNOHANG)
            if wpid == pid:
                rc = _os.waitstatus_to_exitcode(status)
                break
            time.sleep(0.2)
        if rc is None:
            _os.kill(pid, signal.SIGKILL)
            _os.waitpid(pid, 0)
            print("  llm worker timed out — skipping", file=sys.stderr)
            return {}

        if rc != 0:
            err = ""
            try:
                err = open(tmp_err.name).read().strip()
            except OSError:
                pass
            print(
                f"  llm worker exited {rc} — skipping (is mlx-lm installed?)"
                + (f"\n    {err}" if err else ""),
                file=sys.stderr,
            )
            return {}

        with open(tmp_out.name) as f:
            raw = _json.load(f)
        return {url: [c for c in cats] for url, cats in raw.items()}
    except Exception as e:  # noqa: BLE001
        print(f"  llm worker failed: {e}", file=sys.stderr)
        return {}
    finally:
        for p in (tmp_in.name, tmp_out.name, tmp_err.name):
            try:
                _os.unlink(p)
            except OSError:
                pass
