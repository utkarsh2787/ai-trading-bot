"""Zerodha Kite Connect historical-data provider.

Research only: this module calls ``historical_data`` and ``instruments`` and
nothing else. The client is injected (any object with those two methods), so
tests never touch the network.

Limits respected: <= ``max_requests_per_sec`` requests per second and request
windows of <= 60 days (minute) / 2000 days (day).
"""

from __future__ import annotations

import os
import time as _time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import Any, Protocol

import polars as pl

from orb.config import KiteConfig
from orb.data.provider import DataProvider, FatalProviderError, ProviderError, SymbolNotFound
from orb.data.schema import DAILY_SCHEMA, MINUTE_SCHEMA, conform_daily, conform_minute, empty

# kiteconnect exception class names (matched by name to avoid a hard import).
_FATAL = {"TokenException", "PermissionException"}
_NON_RETRYABLE = _FATAL | {"InputException", "OrderException"}


class KiteClient(Protocol):
    def historical_data(
        self, instrument_token: int, from_date: Any, to_date: Any, interval: str, **kw: Any
    ) -> list[dict]: ...

    def instruments(self, exchange: str | None = None) -> list[dict]: ...


class RateLimiter:
    """Spaces calls at least ``1/rate`` seconds apart."""

    def __init__(
        self,
        rate_per_sec: float,
        clock: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self.interval = 1.0 / rate_per_sec
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            delay = self._last + self.interval - now
            if delay > 0:
                self.sleep(delay)
                now += delay
        self._last = now


def date_chunks(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    """Inclusive, non-overlapping windows of at most ``days`` calendar days."""
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


class KiteProvider(DataProvider):
    name = "kite"

    def __init__(
        self,
        client: KiteClient,
        cfg: KiteConfig,
        instruments: dict[str, int] | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = _time.sleep,
    ):
        self.client = client
        self.cfg = cfg
        self._tokens = instruments
        self.limiter = limiter or RateLimiter(cfg.max_requests_per_sec)
        self._sleep = sleep

    # ------------------------------------------------------------ instruments
    def tokens(self) -> dict[str, int]:
        """tradingsymbol -> instrument_token for stocks and indices on the exchange."""
        if self._tokens is None:
            exchanges = {self.cfg.exchange, self.cfg.index_exchange}
            tokens: dict[str, int] = {}
            for ex in sorted(exchanges):
                for row in self._call(self.client.instruments, ex):
                    if row.get("segment") in (ex, "INDICES") or row.get("instrument_type") == "EQ":
                        tokens.setdefault(row["tradingsymbol"], int(row["instrument_token"]))
            self._tokens = tokens
        return self._tokens

    def resolve(self, symbol: str) -> int:
        try:
            return self.tokens()[symbol]
        except KeyError:
            raise SymbolNotFound(
                f"{symbol!r} not in current Kite instruments (delisted or renamed?)"
            ) from None

    # ------------------------------------------------------------------ calls
    def _call(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        attempt = 0
        while True:
            self.limiter.wait()
            try:
                return fn(*args, **kw)
            except Exception as exc:  # noqa: BLE001 - classified below
                kind = type(exc).__name__
                if kind in _FATAL:
                    raise FatalProviderError(f"{kind}: {exc}") from exc
                if kind in _NON_RETRYABLE or attempt >= self.cfg.max_retries:
                    raise ProviderError(f"{kind}: {exc}") from exc
                self._sleep(self.cfg.backoff_base_sec * 2**attempt)
                attempt += 1

    def _history(
        self, symbol: str, start: date, end: date, interval: str, chunk_days: int
    ) -> list[dict]:
        token = self.resolve(symbol)
        rows: list[dict] = []
        for a, b in date_chunks(start, end, chunk_days):
            rows += self._call(
                self.client.historical_data,
                token,
                datetime.combine(a, datetime.min.time()),
                datetime.combine(b, datetime.max.time().replace(microsecond=0)),
                interval,
            )
        return rows

    # -------------------------------------------------------------- interface
    def minute_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        rows = self._history(symbol, start, end, "minute", self.cfg.minute_chunk_days)
        if not rows:
            return empty(MINUTE_SCHEMA)
        df = pl.DataFrame(rows).rename({"date": "ts"}).with_columns(symbol=pl.lit(symbol))
        return conform_minute(df)

    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        rows = self._history(symbol, start, end, "day", self.cfg.day_chunk_days)
        if not rows:
            return empty(DAILY_SCHEMA)
        return conform_daily(pl.DataFrame(rows).with_columns(symbol=pl.lit(symbol)))


def client_from_env(cfg: KiteConfig, data_root: str = "data") -> KiteClient:
    """KiteConnect client from KITE_API_KEY plus the access token from `orb login`
    (or KITE_ACCESS_TOKEN). Login itself is the user's own interactive step."""
    from kiteconnect import KiteConnect  # optional dependency: `uv sync --extra kite`

    from orb.data.kite_auth import KiteAuthError, access_token

    api_key = os.environ.get(cfg.api_key_env, "").strip()
    if not api_key:
        raise FatalProviderError(f"missing environment variable {cfg.api_key_env}")
    try:
        token = access_token(data_root)
    except KiteAuthError as e:
        raise FatalProviderError(str(e)) from None
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    return kite
