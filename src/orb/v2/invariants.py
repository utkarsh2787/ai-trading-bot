"""V2 invariants, checked for every (day, book); any violation fails the run.

  1. taken trades <= max_positions (all are open together from 14:30)
  2. deployed notional <= deploy_fraction x equity_d (no leverage)
  3. entries only on the 14:30..14:35 candles
  4. exits at or before 15:10 (the 15:10 open, or the close of an earlier candle),
     never before the entry
  5. at most one trade per stock per day
  6. no trades once equity_d < ruin_equity
  7. cash never negative: equity_d - sum(notional + entry charges) >= 0
  8. qty >= 1 and notional == qty x entry fill
And across days, per book: equity_{d+1} = equity_d + sum of the day's rounded net.
"""

from __future__ import annotations

from orb.features import slot
from orb.v2.config import ConfigV2
from orb.v2.sizing import DayLimits

TOL = 0.01


class V2InvariantError(AssertionError):
    pass


def violations(taken: list[dict], lim: DayLimits, book: str, cfg: ConfigV2) -> list[str]:
    if not taken:
        return []
    s = cfg.session
    e0, e1, x = slot(s, s.entry_candle), slot(s, s.entry_last), slot(s, s.exit_candle)
    tag = f"{lim.day} {book}"
    out = []
    if len(taken) > cfg.selection.max_positions:
        out.append(f"{tag}: {len(taken)} trades > {cfg.selection.max_positions}")
    dep = sum(t["notional"] for t in taken)
    if dep > lim.deployable + 1e-6:
        out.append(f"{tag}: deployed {dep:.2f} > {lim.deployable:.2f}")
    syms = [t["symbol"] for t in taken]
    if len(syms) != len(set(syms)):
        out.append(f"{tag}: more than one trade for a stock: {sorted(syms)}")
    if lim.ruined:
        out.append(f"{tag}: {len(taken)} trades after ruin (equity {lim.equity:.2f})")
    cash = lim.equity - sum(t["notional"] + t["entry_costs"] for t in taken)
    if cash < -TOL:
        out.append(f"{tag}: cash negative ({cash:.2f})")
    for t in taken:
        sym = t["symbol"]
        if not e0 <= t["entry_slot"] <= e1:
            out.append(f"{tag}: {sym} entry slot {t['entry_slot']} outside 14:30-14:35")
        late = t["exit_slot"] > x or (t["exit_at"] == "close" and t["exit_slot"] >= x)
        if late or t["exit_slot"] < t["entry_slot"]:
            out.append(f"{tag}: {sym} exit {t['exit_at']} of slot {t['exit_slot']} invalid")
        if t["qty"] < 1 or abs(t["notional"] - t["qty"] * t["entry_price"]) > 1e-6:
            out.append(f"{tag}: {sym} qty {t['qty']} / notional {t['notional']} inconsistent")
    return out


def check(taken: list[dict], lim: DayLimits, book: str, cfg: ConfigV2) -> None:
    v = violations(taken, lim, book, cfg)
    if v:
        raise V2InvariantError("\n".join(v))


def check_equity_path(rows: list[dict], cfg: ConfigV2) -> None:
    """``rows``: one book's equity rows in date order."""
    eq = cfg.sizing.starting_capital
    for r in rows:
        if abs(r["equity_start"] - eq) > TOL:
            raise V2InvariantError(f"{r['date']} {r['book']}: equity {r['equity_start']} != {eq}")
        eq = r["equity_start"] + r["day_net_rounded"]
        if abs(r["equity_end"] - eq) > TOL:
            raise V2InvariantError(f"{r['date']} {r['book']}: equity_end != start + day net")
