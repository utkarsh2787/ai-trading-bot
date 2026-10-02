from datetime import date, datetime, timedelta, timezone

import pytest

from orb.data.kite import KiteProvider, RateLimiter, date_chunks
from orb.data.provider import FatalProviderError, ProviderError, SymbolNotFound

IST = timezone(timedelta(hours=5, minutes=30))


class NetworkException(Exception):
    pass


class TokenException(Exception):
    pass


class InputException(Exception):
    pass


class FakeKite:
    """Mimics kiteconnect: tz-aware datetimes under 'date', one bar per weekday 09:15."""

    def __init__(self, fail_first: int = 0, exc: type[Exception] = NetworkException):
        self.calls: list[tuple] = []
        self.fail_first = fail_first
        self.exc = exc

    def instruments(self, exchange=None):
        return [
            {
                "tradingsymbol": "RELIANCE",
                "instrument_token": 738561,
                "segment": "NSE",
                "instrument_type": "EQ",
            },
            {
                "tradingsymbol": "NIFTY 200",
                "instrument_token": 264457,
                "segment": "INDICES",
                "instrument_type": "EQ",
            },
        ]

    def historical_data(self, instrument_token, from_date, to_date, interval, **kw):
        self.calls.append((instrument_token, from_date, to_date, interval))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise self.exc("boom")
        out = []
        d = from_date.date()
        while d <= to_date.date():
            if d.weekday() < 5:
                ts = (
                    datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
                    if interval == "minute"
                    else datetime(d.year, d.month, d.day, tzinfo=IST)
                )
                out.append(
                    {
                        "date": ts,
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.0,
                        "close": 10.5,
                        "volume": 100,
                    }
                )
            d += timedelta(days=1)
        return out


def _provider(cfg, client, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return KiteProvider(client, cfg.data.kite, limiter=RateLimiter(1e9), sleep=sleeps.append)


def test_date_chunks_cover_range_without_overlap():
    chunks = list(date_chunks(date(2024, 1, 1), date(2024, 5, 1), 60))
    assert chunks[0][0] == date(2024, 1, 1) and chunks[-1][1] == date(2024, 5, 1)
    for (a, b), (c, _) in zip(chunks, chunks[1:], strict=False):
        assert (b - a).days <= 59 and c == b + timedelta(days=1)


def test_minute_requests_chunked_to_60_days(cfg):
    client = FakeKite()
    df = _provider(cfg, client).minute_bars("RELIANCE", date(2024, 1, 1), date(2024, 6, 30))
    assert len(client.calls) == 4
    assert all(c[3] == "minute" and (c[2] - c[1]).days < 60 for c in client.calls)
    assert df["ts"].dtype.time_zone == "Asia/Kolkata"
    assert df["ts"][0] == datetime(2024, 1, 1, 9, 15, tzinfo=IST)
    assert df["symbol"].unique().to_list() == ["RELIANCE"]


def test_daily_bars_and_index_token(cfg):
    client = FakeKite()
    df = _provider(cfg, client).daily_bars("NIFTY 200", date(2024, 1, 1), date(2024, 1, 5))
    assert client.calls[0][0] == 264457 and client.calls[0][3] == "day"
    assert df["date"].to_list() == [date(2024, 1, d) for d in range(1, 6)]


def test_unknown_symbol_flagged(cfg):
    with pytest.raises(SymbolNotFound, match="delisted"):
        _provider(cfg, FakeKite()).minute_bars("MINDTREE", date(2024, 1, 1), date(2024, 1, 2))


def test_transient_errors_retried_with_backoff(cfg):
    sleeps: list[float] = []
    client = FakeKite(fail_first=2)
    df = _provider(cfg, client, sleeps).daily_bars("RELIANCE", date(2024, 1, 1), date(2024, 1, 3))
    assert df.height == 3
    b = cfg.data.kite.backoff_base_sec
    assert sleeps == [b, 2 * b]


def test_retries_exhausted(cfg):
    client = FakeKite(fail_first=99)
    with pytest.raises(ProviderError):
        _provider(cfg, client).daily_bars("RELIANCE", date(2024, 1, 1), date(2024, 1, 3))
    assert len(client.calls) == cfg.data.kite.max_retries + 1


def test_auth_error_is_fatal_and_not_retried(cfg):
    client = FakeKite(fail_first=1, exc=TokenException)
    with pytest.raises(FatalProviderError):
        _provider(cfg, client).daily_bars("RELIANCE", date(2024, 1, 1), date(2024, 1, 3))
    assert len(client.calls) == 1


def test_input_error_not_retried(cfg):
    client = FakeKite(fail_first=1, exc=InputException)
    with pytest.raises(ProviderError):
        _provider(cfg, client).daily_bars("RELIANCE", date(2024, 1, 1), date(2024, 1, 3))
    assert len(client.calls) == 1


def test_rate_limiter_spaces_calls():
    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(3, clock=lambda: t[0], sleep=sleep)
    for _ in range(4):
        rl.wait()
    assert slept == pytest.approx([1 / 3] * 3)
