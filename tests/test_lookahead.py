"""Look-ahead tests: truncate (or scramble) all data after the signal candle t
and assert the signal and every feature are unchanged.

"After t" means: minute bars of the trade date T with ts > t (stocks AND index),
every minute bar of later days, T's own daily bar (bhavcopy is published after
the close) and every later daily bar, plus a corporate action after T.
"""

from datetime import timedelta

import numpy as np
import polars as pl
import pytest

from orb.scoring import RuleScorer
from orb.signals import scan_day
from tests.market import random_market

COMPARE = [
    "symbol",
    "signal_ts",
    "side",
    "or_h",
    "or_l",
    "or_candles",
    "close_t",
    "atr14",
    "prev_close",
    "d",
    "rv",
    "rv_sessions",
    "r_idx",
    "index_used",
    "w",
    "v",
    "s_breakout",
    "s_rel_volume",
    "s_market",
    "s_or_quality",
    "s_volatility",
    "score",
    "decision",
    "reason",
]


@pytest.fixture(scope="module")
def market(cfg):
    return random_market(cfg)


def _scan(m, day):
    return scan_day(m.builder().day(day), m.cfg, RuleScorer(m.cfg.scoring))


def _same(a: dict, b: dict):
    for k in COMPARE:
        x, y = a[k], b[k]
        if isinstance(x, float) and y is not None:
            assert x == pytest.approx(y, rel=1e-9, abs=1e-12), k
        else:
            assert x == y, k


def _truncated(m, day, t_ts, scramble: bool, rng):
    """Copy of the market with everything after (day, t) removed or scrambled."""
    future = (pl.col("ts").dt.date() > day) | (
        (pl.col("ts").dt.date() == day) & (pl.col("ts") > t_ts)
    )

    def cut(df):
        if not scramble:
            return df.filter(~future)
        noise = pl.Series(rng.uniform(0.5, 1.5, df.height))
        return df.with_columns(
            *(
                pl.when(future).then(pl.col(c) * noise).otherwise(pl.col(c)).alias(c)
                for c in ("open", "high", "low", "close")
            ),
            pl.when(future)
            .then((pl.col("volume") * noise * 3).cast(pl.Int64))
            .otherwise(pl.col("volume"))
            .alias("volume"),
        )

    daily = (
        m.daily.filter(pl.col("date") < day)
        if not scramble
        else m.daily.with_columns(
            *(
                pl.when(pl.col("date") >= day).then(pl.col(c) * 1.7).otherwise(pl.col(c)).alias(c)
                for c in ("open", "high", "low", "close")
            )
        )
    )
    actions = m.actions
    if scramble:  # a split announced for after T must not leak back
        actions = pl.DataFrame(
            {
                "symbol": m.symbols,
                "ex_date": [day + timedelta(days=3)] * len(m.symbols),
                "action_type": "split",
                "price_factor": 0.5,
            }
        )
    return type(m)(
        m.cfg,
        m.calendar,
        daily,
        cut(m.minute),
        cut(m.index_minute),
        actions,
        m.excluded,
        m.invalid_daily,
        m.index_invalid,
    )


@pytest.mark.parametrize("scramble", [False, True], ids=["truncate", "scramble"])
def test_features_and_signal_unchanged_after_t(market, scramble):
    rng = np.random.default_rng(0)
    days = market.calendar[-6:]
    checked = 0
    for day in days:
        full = _scan(market, day)
        for row in full.to_dicts():
            alt = _truncated(market, day, row["signal_ts"], scramble, rng)
            got = _scan(alt, day).filter(pl.col("symbol") == row["symbol"])
            assert got.height == 1, f"signal vanished for {row['symbol']} {day}"
            _same(got.row(0, named=True), row)
            checked += 1
    assert checked >= 8, "scenario should produce enough breakouts to be meaningful"


def test_no_signal_before_first_breakout_appears_when_truncated(market):
    """Truncating at any earlier candle must never create a breakout before the real one."""
    day = market.calendar[-1]
    full = _scan(market, day)
    for row in full.to_dicts():
        early = row["signal_ts"] - timedelta(minutes=1)
        alt = _truncated(market, day, early, False, None)
        got = _scan(alt, day).filter(pl.col("symbol") == row["symbol"])
        assert got.height == 0
