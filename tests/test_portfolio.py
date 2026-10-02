from datetime import date

import polars as pl

from orb.portfolio import SKIPPED, TAKEN, allocate

D = date(2024, 1, 5)


def day(rows):
    base = {
        "date": D,
        "decision": "QUALIFIED",
        "reason": None,
        "status": "OK",
        "qty": 10,
        "notional": 2000.0,
        "score": 80.0,
        "exit_slot": 355,
    }
    return pl.DataFrame([{**base, **r} for r in rows])


def result(df):
    return {r["symbol"]: (r["book_decision"], r["book_reason"]) for r in df.to_dicts()}


def test_same_minute_ties_by_score_then_slots_full(cfg):
    out = allocate(
        day(
            [
                {"symbol": "A", "entry_slot": 30, "score": 70.0},
                {"symbol": "B", "entry_slot": 30, "score": 90.0},
                {"symbol": "C", "entry_slot": 30, "score": 80.0},
                {"symbol": "D", "entry_slot": 30, "score": 80.0},  # ties C on score -> symbol order
            ]
        ),
        cfg.portfolio,
    )
    assert result(out) == {
        "B": (TAKEN, None),
        "C": (TAKEN, None),
        "D": (TAKEN, None),
        "A": (SKIPPED, "SLOTS_FULL"),
    }


def test_slot_freed_only_after_exit_candle(cfg):
    rows = [{"symbol": s, "entry_slot": 30, "exit_slot": 40} for s in "ABC"]
    same = allocate(day(rows + [{"symbol": "X", "entry_slot": 40}]), cfg.portfolio)
    assert result(same)["X"] == (SKIPPED, "SLOTS_FULL")  # exit in candle 40, entry at 40
    after = allocate(day(rows + [{"symbol": "X", "entry_slot": 41}]), cfg.portfolio)
    assert result(after)["X"] == (TAKEN, None)


def test_non_qualified_and_untradeable(cfg):
    out = allocate(
        day(
            [
                {
                    "symbol": "R",
                    "entry_slot": 30,
                    "decision": "REJECTED",
                    "reason": "SCORE_BELOW_THRESHOLD",
                },
                {"symbol": "G", "entry_slot": 30, "status": "INVALID_ENTRY_GAP"},
                {"symbol": "Q", "entry_slot": 30, "qty": 0},
                {"symbol": "E", "entry_slot": 30, "decision": "EXCLUDED", "reason": "EXCLUDED_BAN"},
            ]
        ),
        cfg.portfolio,
    )
    assert result(out) == {
        "R": ("REJECTED", "SCORE_BELOW_THRESHOLD"),
        "G": (SKIPPED, "INVALID_ENTRY_GAP"),
        "Q": (SKIPPED, "QTY_BELOW_ONE"),
        "E": ("EXCLUDED", "EXCLUDED_BAN"),
    }


def test_max_deployment(cfg):
    out = allocate(
        day(
            [
                {"symbol": "A", "entry_slot": 30, "notional": 5000.0},
                {"symbol": "B", "entry_slot": 31, "notional": 3500.0},  # 8500 > 8000
            ]
        ),
        cfg.portfolio,
    )
    assert result(out)["B"] == (SKIPPED, "MAX_DEPLOYMENT")


def test_later_signals_never_change_earlier_decisions(cfg):
    early = [{"symbol": s, "entry_slot": 30 + i, "exit_slot": 50} for i, s in enumerate("AB")]
    a = result(allocate(day(early), cfg.portfolio))
    b = result(
        allocate(
            day(
                early
                + [
                    {"symbol": "Z", "entry_slot": 31, "score": 99.0},
                    {"symbol": "Y", "entry_slot": 200},
                ]
            ),
            cfg.portfolio,
        )
    )
    assert {k: b[k] for k in a} == a
