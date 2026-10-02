"""Polite, cached HTTP for public NSE / niftyindices.com files.

* rate-limited (``nse.max_requests_per_sec``), browser User-Agent, retries;
* ``www.nseindia.com/api/...`` currently answers without cookies; if it starts
  refusing (401/403) the home page is fetched once for session cookies
  (that page itself may 403 non-browser clients, which is tolerated);
* every response is cached under ``cache_dir`` keyed by URL, and 404s are
  cached as ``.404`` markers (e.g. holidays have no ban file), so reruns are
  resumable and offline-reproducible.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from orb.config import NSEConfig
from orb.data.kite import RateLimiter

log = logging.getLogger(__name__)


class Fetcher(Protocol):
    def get(self, url: str) -> bytes | None:
        """Body, or None if the resource does not exist (404)."""
        ...


class HttpError(Exception):
    pass


class NSEHttp:
    def __init__(self, cfg: NSEConfig, sleep=time.sleep):
        self.cfg = cfg
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.limiter = RateLimiter(cfg.max_requests_per_sec)
        self.sleep = sleep

    def _open(self, url: str) -> bytes | None:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.cfg.user_agent,
                "Accept": "*/*",
                "Referer": self.cfg.www_base + "/",
            },
        )
        for attempt in range(self.cfg.max_retries + 1):
            self.limiter.wait()
            try:
                with self.opener.open(req, timeout=self.cfg.timeout_sec) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code < 500 and e.code != 429 and e.code != 403:
                    raise HttpError(f"{e.code} {url}") from e
                err: Exception = e
            except (urllib.error.URLError, TimeoutError) as e:
                err = e
            log.warning("retry %d %s: %s", attempt + 1, url, err)
            self.sleep(2.0**attempt)
            if url.startswith(self.cfg.www_base + "/api"):
                self._warm()
        raise HttpError(f"giving up on {url}: {err}")

    def _warm(self) -> None:
        """Best-effort cookie fetch; never fails the caller."""
        req = urllib.request.Request(
            self.cfg.www_base + "/", headers={"User-Agent": self.cfg.user_agent, "Accept": "*/*"}
        )
        try:
            self.limiter.wait()
            with self.opener.open(req, timeout=self.cfg.timeout_sec) as r:
                r.read()
        except Exception as exc:  # noqa: BLE001
            log.info("cookie warm-up failed (ignored): %s", exc)

    def get(self, url: str) -> bytes | None:
        return self._open(url)


class CachedFetcher:
    def __init__(self, inner: Fetcher, cache_dir: str | Path, refresh: bool = False):
        self.inner = inner
        self.dir = Path(cache_dir)
        self.refresh = refresh

    def _path(self, url: str) -> Path:
        name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "index"
        digest = hashlib.sha1(url.encode()).hexdigest()[:10]
        return self.dir / f"{digest}_{name}"

    def get(self, url: str) -> bytes | None:
        p = self._path(url)
        miss = p.with_name(p.name + ".404")
        if not self.refresh:
            if p.exists():
                return p.read_bytes()
            if miss.exists():
                return None
        body = self.inner.get(url)
        self.dir.mkdir(parents=True, exist_ok=True)
        if body is None:
            miss.touch()
            return None
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(p)
        return body
