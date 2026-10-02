"""Signal scan: golden day, filters, rejection reasons, scorer independence."""

from datetime import date

import numpy as np
import polars as pl
import pytest

from orb.scoring import RuleScorer
from orb.signals import FILTERED, QUALIFIED, REJECTED, scan_day
from tests.market import Market, bars_from_arrays, flat_daily, weekdays

N = 375


def golden_market(
    cfg, or_hi=101.0, breakout_close=101.3, today_vol=2500, index_move=0.002, atr=3.0, n_days=130
):
    """OR 100..101, breakout at 09:44 (slot 29) closing at 101.3, ATR 3, prev close 100,
    RV 2.5, index +0.2% -> every factor at its maximum."""
    cal = weekdays(date(2024, 1, 1), n_days)
    T = cal[-1]
    minute = []
    for d in cal[-31:-1]:  # 30 baseline sessions, 1000 shares/min
        c = np.full(N, 100.5)
        minute.append(bars_from_arrays("AAA", d, c, c + 0.2, c - 0.2, c, np.full(N, 1000)))
    c = np.full(N, 100.5)
    h, lo = c + 0.1, c - 0.1
    h[:15], lo[:15] = or_hi, 100.0  # OR candles span 100 .. or_hi
    c[29] = breakout_close
    h[29] = max(h[29], breakout_close)
    minute.append(bars_from_arrays("AAA", T, c, h, lo, c, np.full(N, today_vol)))
    idx = []
    for d in cal[-31:]:
        o = np.full(N, 20000.0)
        ic = o * (1 + (index_move if d == T else 0.0))
        idx.append(
            bars_from_arrays(
                "NIFTY 200", d, o, np.maximum(o, ic) + 1, np.minimum(o, ic) - 1, ic, np.zeros(N)
            )
        )
    return Market(
        cfg, cal, flat_daily("AAA", cal, close=100.0, rng=atr), pl.concat(minute), pl.concat(idx)
    ), T


def scan(m: Market, day, scorer=None):
    return scan_day(m.builder().day(day), m.cfg, scorer or RuleScorer(m.cfg.scoring))


def test_golden_day(cfg):
    m, T = golden_market(cfg)
    out = scan(m, T)
    assert out.height == 1
    r = out.row(0, named=True)
    assert r["side"] == "long" and r["signal_slot"] == 29
    assert r["signal_ts"].hour == 9 and r["signal_ts"].minute == 44
    assert r["or_h"] == 101.0 and r["or_l"] == 100.0
    assert r["atr14"] == pytest.approx(3.0) and r["prev_close"] == pytest.approx(100.0)
    assert r["d"] == pytest.approx(0.1) and r["w"] == pytest.approx(1 / 3)
    assert r["v"] == pytest.approx(0.03) and r["rv"] == pytest.approx(2.5)
    assert r["r_idx"] == pytest.approx(0.002) and r["index_used"] == "NIFTY 200"
    assert r["rv_sessions"] == 20 and not r["index_substituted"]
    assert r["score"] == pytest.approx(100.0) and r["decision"] == QUALIFIED


def test_score_below_threshold_rejected(cfg):
    m, T = golden_market(cfg, today_vol=1000, index_move=-0.002, breakout_close=101.03)
    r = scan(m, T).row(0, named=True)
    # d=0.01 -> 3, RV=1 -> 0, market against -> 0, OR 15, vol 10 => 28
    assert r["score"] == pytest.approx(28.0)
    assert (r["decision"], r["reason"]) == (REJECTED, "SCORE_BELOW_THRESHOLD")


def test_or_width_hard_filter_still_scored(cfg):
    m, T = golden_market(cfg, or_hi=104.0, breakout_close=104.3)  # w = 4/3 > 1
    r = scan(m, T).row(0, named=True)
    assert (r["decision"], r["reason"]) == (FILTERED, "OR_WIDTH")
    assert r["score"] is not None and r["s_or_quality"] == 0


def test_insufficient_history_is_its_own_reason(cfg):
    m, T = golden_market(cfg, n_days=60)  # < 100 daily bars
    r = scan(m, T).row(0, named=True)
    assert (r["decision"], r["reason"]) == (REJECTED, "INSUFFICIENT_HISTORY:daily_bars")
    # RV: exclude 16 of the 30 window sessions -> only 14 valid
    m2, T2 = golden_market(cfg)
    m2.excluded = pl.DataFrame({"symbol": "AAA", "date": m2.calendar[-31:-15]})
    r2 = scan(m2, T2).row(0, named=True)
    assert r2["reason"] == "INSUFFICIENT_HISTORY:rv_sessions" and r2["rv_sessions"] == 14


def test_excluded_rv_days_skipped_not_counted(cfg):
    m, T = golden_market(cfg)
    # make the 3 most recent sessions huge-volume, then exclude them
    recent = m.calendar[-4:-1]
    m.minute = m.minute.with_columns(
        volume=pl.when(pl.col("ts").dt.date().is_in(recent)).then(10**6).otherwise("volume")
    )
    m.excluded = pl.DataFrame({"symbol": "AAA", "date": recent})
    r = scan(m, T).row(0, named=True)
    assert r["rv"] == pytest.approx(2.5) and r["rv_sessions"] == 20


def test_index_fallback_and_missing(cfg):
    m, T = golden_market(cfg)
    n50 = m.index_minute.with_columns(symbol=pl.lit("NIFTY 50"))
    m.index_minute = pl.concat([m.index_minute.filter(pl.col("ts").dt.date() != T), n50])
    r = scan(m, T).row(0, named=True)
    assert r["index_used"] == "NIFTY 50" and r["index_substituted"]
    m.index_invalid = {("NIFTY 50", T)}
    r = scan(m, T).row(0, named=True)
    assert (r["decision"], r["reason"]) == (REJECTED, "MARKET_DATA_MISSING")


def test_excluded_stock_day_not_scanned(cfg):
    m, T = golden_market(cfg)
    m.excluded = pl.DataFrame({"symbol": ["AAA"], "date": [T]})
    assert scan(m, T).height == 0
    m.excluded = pl.DataFrame({"symbol": ["*"], "date": [T]})  # whole-market day
    assert scan(m, T).height == 0


class ConstantScorer:
    """Not a RuleScorer: the scanner must only need the Scorer protocol."""

    name = "const"

    def score(self, features):
        assert set(features) == {"side", "d", "rv", "r_idx", "w", "v"}
        return 70.0


def test_scanner_depends_only_on_scorer_interface(cfg):
    m, T = golden_market(cfg, today_vol=1000)
    r = scan(m, T, ConstantScorer()).row(0, named=True)
    assert r["score"] == 70 and r["scorer"] == "const" and r["decision"] == QUALIFIED
    assert r["s_breakout"] is None  # no explain() -> no components
