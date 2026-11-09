"""Shared helpers: config, paths, JSON storage, HTTP with retries and rate limiting."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("AGENT_DATA_DIR", ROOT / "data"))
WSOL = "So11111111111111111111111111111111111111112"

log = logging.getLogger("agent")


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")


def load_config(path: str | os.PathLike | None = None) -> dict:
    with open(path or ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)
    tmp.replace(path)


class RateLimiter:
    """Simple thread-safe limiter: at most `rps` calls per second."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, rps: float = 4, timeout: float = 25):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "solana-wallet-agent/1.0"
        self.limiter = RateLimiter(rps)
        self.timeout = timeout

    def request(self, method: str, url: str, retries: int = 4, **kw):
        """Returns parsed JSON, or None on 404 / persistent failure."""
        for attempt in range(retries + 1):
            self.limiter.wait()
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                log.debug("http error %s %s", url, e)
                r = None
            if r is not None:
                if r.status_code == 404:
                    return None
                if r.ok:
                    try:
                        return r.json()
                    except ValueError:
                        return None
                if r.status_code not in (408, 425, 429, 500, 502, 503, 504):
                    log.warning("HTTP %s from %s: %s", r.status_code, url.split("?")[0], r.text[:200])
                    return None
            time.sleep(min(2 ** attempt, 20))
        log.warning("giving up on %s", url.split("?")[0])
        return None

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)
