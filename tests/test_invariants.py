"""Each invariant must fire on a tampered copy of the golden day's real book."""

import polars as pl
import pytest

from orb import invariants
from orb.engine import Engine
from orb.portfolio import TAKEN
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.test_engine_golden import golden_day


@pytest.fixture(scope="module")
def book(cfg, tmp_path_factory):
    m, T = golden_day(cfg)
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, cfg.ticks).to_dicts()
    }
    res = Engine(cfg, m.builder(), ticks, RuleScorer(cfg.scoring)).run(
        T, T, ledger=tmp_path_factory.mktemp("l") / "l.jsonl"
    )
    return res.book.filter((pl.col("variant") == "default") & (pl.col("slippage_mult") == 1.0))


def tamper(book, sym, **cols):
    return book.with_columns(
        *(
            pl.when(pl.col("symbol") == sym).then(pl.lit(v)).otherwise(pl.col(k)).alias(k)
            for k, v in cols.items()
        )
    )


def test_real_book_is_clean(book, cfg):
    assert invariants.violations(book, cfg) == []


@pytest.mark.parametrize(
    "sym, cols, msg",
    [
        (
            "D",
            {
                "book_decision": TAKEN,
                "entry_slot": 31,
                "exit_slot": 300,
                "notional": 2000.0,
                "qty": 19,
                "initial_risk": 10.0,
                "gross_pnl": 0.0,
                "costs": 0.0,
                "net_pnl": 0.0,
                "costs_rounded": 0.0,
                "net_pnl_rounded": 0.0,
            },
            "positions open",
        ),
        ("A", {"notional": 6000.0}, "deployed"),
        ("B", {"exit_slot": 356}, "after the hard exit"),
        ("A", {"signal_slot": 316, "entry_slot": 317}, "entry window"),
        ("A", {"initial_risk": 100.5}, "risk"),
        ("A", {"net_pnl": 0.0}, "equity change"),
        ("C", {"net_pnl_rounded": -61.70}, "equity change"),
        ("A", {"notional": 9999.0, "exit_slot": 31}, "cash negative"),
    ],
)
def test_each_invariant_fires(book, cfg, sym, cols, msg):
    v = invariants.violations(tamper(book, sym, **cols), cfg)
    assert any(msg in x for x in v), v
    with pytest.raises(invariants.EngineInvariantError):
        invariants.check(tamper(book, sym, **cols), cfg)


def test_one_trade_per_stock(book, cfg):
    dup = pl.concat([book, book.filter(pl.col("symbol") == "B")])
    assert any("more than one trade" in x for x in invariants.violations(dup, cfg))
