"""A shared, rate-limited HTTP session.

Every outbound request in the tool goes through here so the politeness
guarantees (per-host spacing, identifying User-Agent, bounded retries) hold
uniformly — there is no back door that skips the rate limiter.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional
from urllib.parse import urlparse

import requests


class PoliteSession:
    """A requests.Session wrapper that enforces a minimum interval per host."""

    def __init__(self, user_agent: str, min_interval: float, timeout: float,
                 max_retries: int):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._min_interval = min_interval
        self._timeout = timeout
        self._max_retries = max_retries
        self._last_hit: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            last = self._last_hit.get(host)
            if last is not None:
                wait = self._min_interval - (time.monotonic() - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    def get(self, url: str, *, accept: Optional[str] = None,
            allow_redirects: bool = True, stream: bool = False) -> requests.Response:
        headers = {"Accept": accept} if accept else None
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            self._throttle(url)
            try:
                resp = self._session.get(
                    url, headers=headers, timeout=self._timeout,
                    allow_redirects=allow_redirects, stream=stream,
                )
                # Back off and retry on transient/server-side throttling.
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = _retry_after_seconds(resp)
                    time.sleep(retry_after if retry_after is not None
                               else self._min_interval * (attempt + 2))
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self._min_interval * (attempt + 2))
        if last_exc is not None:
            raise last_exc
        # Return the last (throttled) response if we exhausted retries on 5xx/429.
        return resp

    def close(self) -> None:
        self._session.close()


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
