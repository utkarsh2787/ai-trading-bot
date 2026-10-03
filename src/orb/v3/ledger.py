"""V3 portfolio ledger: holdings, cash, corporate actions, dividends, rebalancing.

One daily loop, used unchanged by every slippage book and by every null draw
(only the ``selector`` differs: the ranked target set, or 2 random eligible
stocks). Per trading day, in order:

  1. corporate actions on held stocks with ex-date since the previous trading
     day: dividends credited (qty x Rs per share, before any same-day split),
     splits / bonuses (qty floored, fraction dropped), rights held through
  2. delisting / merger: the last traded day -> sell at the official close
  3. carried exits (from 09:20), then deferred exits (15:00, if not DQ-excluded)
  4. demerger on the next trading day -> sell at today's 15:00 (EXIT_CORP_ACTION)
  5. rebalance day: equity_w at 14:59, ruin check, Slot_w = 0.9 x equity_w / 2;
     sells (held, not in the target set), then buys in rank order into free slots;
     the last rebalance on or before the in-sample end sells everything (FORCED_END)
  6. contract-note rounding of the day's STT / stamp; DP per stock per sell day
  7. invariants; mark-to-market at the official close
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from orb.v2.config import SlippageModel
from orb.v2.trade import fill
from orb.v3 import invariants
from orb.v3.costs import CHARGES, CnCOrder, dp_charge, order_charges, round_day
from orb.v3.fills import OFFICIAL, Exec, minute_time
from orb.v3.market import Market

REBALANCE = "REBALANCE"
FORCED_END = "FORCED_END"
EXIT_CORP_ACTION = "EXIT_CORP_ACTION"
EXIT_DELISTED = "EXIT_DELISTED"
EXIT_CARRIED = "EXIT_CARRIED"
EXIT_DEFERRED = "EXIT_DEFERRED"
QTY_ZERO = "QTY_ZERO"
CASH_LIMITED = "CASH_LIMITED"
NO_TICK = "NO_TICK"
RUIN = "RUIN"
CORP_ACTION_NEXT_DAY = "CORP_ACTION_NEXT_DAY"  # target with a demerger tomorrow: not bought
DIVIDEND_NO_AMOUNT = "DIVIDEND_NO_AMOUNT"
FRACTION_DROPPED = "FRACTION_DROPPED"
RIGHTS_HELD_THROUGH = "RIGHTS_HELD_THROUGH"
CASH_RESERVE = 1.0  # kept back on a buy for the day's STT / stamp rounding

Selector = Callable[[date], list[str]]


@dataclass
class Position:
    symbol: str
    qty: int
    entry_qty: int
    entry_day: date
    entry_slot: int
    entry_raw: float
    entry_fill: float
    entry_charges: dict
    entry_cost_u: float
    slot_w: float
    entry_cost_r: float | None = None
    dividends: float = 0.0
    dropped_fraction: float = 0.0
    rights_held: int = 0
    pending: str | None = None  # EXIT_CARRIED | EXIT_DEFERRED
    cause: str | None = None
    flags: list = field(default_factory=list)


@dataclass
class LedgerResult:
    book: str
    trades: list[dict]
    orders: list[dict]
    daily: list[dict]
    rebalances: list[dict]
    counts: Counter
    ruin_date: date | None
    final_equity: float
    year_end: dict[int, float]


def _days_between(a: date | None, b: date):
    """Calendar dates in (a, b] (just b when a is None)."""
    d = b if a is None else a + timedelta(days=1)
    while d <= b:
        yield d
        d += timedelta(days=1)


class Ledger:
    def __init__(
        self,
        market: Market,
        model: SlippageModel,
        name: str,
        selector: Selector,
        record: bool = True,
        check: bool = True,
    ):
        self.m = market
        self.data = market.data
        self.cfg = market.cfg
        self.model = model
        self.name = name
        self.selector = selector
        self.record = record
        self.check = check

    # ------------------------------------------------------------ helpers
    def _tick(self, s: str, d: date) -> float | None:
        t = self.data.ticks.get((s, d))
        if t is None:
            p = self.data.last_close_before(s, d)
            t = self.data.ticks.get((s, p[0])) if p else None
        return t

    def _sell(self, s: str, p: Position, ex: Exec, d: date, cause: str, flags: list) -> dict:
        tick = self._tick(s, d) or 0.05
        px = fill(ex.raw, "sell", tick, self.model)
        ch = order_charges(CnCOrder(d, "sell", p.qty, px), self.cfg.costs)
        dp = dp_charge(self.cfg.costs, d)
        cost_u = sum(ch.values()) + dp
        self.cash += p.qty * px - cost_u
        fl = [*p.flags, *flags] + ([ex.reason] if ex.reason else [])
        o = {
            "day": d,
            "symbol": s,
            "side": "sell",
            "qty": p.qty,
            "raw": ex.raw,
            "fill": px,
            "slot": ex.slot,
            "cause": cause,
            "flags": fl,
            "charges": ch,
            "dp": dp,
            "cost_u": cost_u,
            "position": p,
        }
        self.orders_today.append(o)
        del self.pos[s]
        return o

    def _try_sell(self, s: str, p: Position, d: date, cause: str, carry=False, guard=True):
        ex = self.m.sell(s, d, carry=carry, guard=guard)
        flags = [EXIT_CARRIED] if carry else []
        if p.pending == EXIT_DEFERRED:
            flags.append(EXIT_DEFERRED)
        if not ex.ok:  # locked to the close, or not traded at all -> carry
            p.pending, p.cause = EXIT_CARRIED, cause
            if EXIT_DEFERRED in flags and EXIT_DEFERRED not in p.flags:
                p.flags.append(EXIT_DEFERRED)
            return None
        p.pending = None
        return self._sell(s, p, ex, d, cause, flags)

    def _buy(self, s: str, d: date, slot_w: float) -> str | None:
        """Returns a skip reason, or None when bought."""
        ex = self.m.buy(s, d)
        if not ex.ok:
            return ex.reason
        tick = self._tick(s, d)
        if tick is None:
            return NO_TICK
        px = fill(ex.raw, "buy", tick, self.model)
        qty = math.floor(slot_w / px + 1e-9)
        if qty < 1:
            return QTY_ZERO
        limited = False
        while qty >= 1:
            ch = order_charges(CnCOrder(d, "buy", qty, px), self.cfg.costs)
            if qty * px + sum(ch.values()) + CASH_RESERVE <= self.cash:
                break
            qty -= 1
            limited = True
        if qty < 1:
            return CASH_LIMITED
        cost_u = sum(ch.values())
        self.cash -= qty * px + cost_u
        p = Position(s, qty, qty, d, ex.slot, ex.raw, px, ch, cost_u, slot_w)
        if limited:
            p.flags.append(CASH_LIMITED)
            self.counts[CASH_LIMITED] += 1
        self.pos[s] = p
        self.orders_today.append(
            {
                "day": d,
                "symbol": s,
                "side": "buy",
                "qty": qty,
                "raw": ex.raw,
                "fill": px,
                "slot": ex.slot,
                "cause": "REBALANCE",
                "flags": list(p.flags),
                "charges": ch,
                "dp": 0.0,
                "cost_u": cost_u,
                "position": p,
            }
        )
        return None

    def _mtm(self, d: date) -> float:
        v = self.cash
        for s, p in self.pos.items():
            c = self.data.close(s, d)
            if c is None:
                lc = self.data.last_close_before(s, d)
                c = lc[1] if lc else 0.0
            v += p.qty * c
        return v

    # ---------------------------------------------------------------- run
    def run(self, start: date, end: date) -> LedgerResult:
        cfg, data = self.cfg, self.data
        rebs = self.m.rebalances(start, end)
        self.cash = cfg.sizing.starting_capital
        self.pos: dict[str, Position] = {}
        self.counts = Counter()
        trades, orders, daily, reb_rows, year_end = [], [], [], [], {}
        ruin = None
        if not rebs:
            return LedgerResult(self.name, [], [], [], [], self.counts, None, self.cash, {})
        reb_set, last_reb = set(rebs), rebs[-1]
        days = [d for d in data.trading_days if d >= rebs[0]]
        prev_td = None
        last_day = rebs[0]
        for d in days:
            if d > last_reb and not self.pos:
                break
            last_day = d
            self.orders_today = []
            cash_start, divs = self.cash, 0.0
            # 1. corporate actions on holdings
            for s, p in list(self.pos.items()):
                for cd in _days_between(prev_td, d):
                    dv = data.dividends.get((s, cd))
                    if dv:
                        amt, ok = dv
                        self.cash += p.qty * amt
                        p.dividends += p.qty * amt
                        divs += p.qty * amt
                        if not ok:
                            self.counts[DIVIDEND_NO_AMOUNT] += 1
                    for ex, pf in data.splits.get(s, []):
                        if ex == cd:
                            new = math.floor(p.qty / pf + 1e-9)
                            frac = p.qty / pf - new
                            if frac > 1e-9:
                                p.dropped_fraction += frac
                                self.counts[FRACTION_DROPPED] += 1
                            p.qty = new
                    if (s, cd) in data.rights:
                        p.rights_held += 1
                        self.counts[RIGHTS_HELD_THROUGH] += 1
            # 2. delisting / merger
            for s, p in list(self.pos.items()):
                if data.delisted_on(s) == d:
                    self._sell(s, p, Exec(True, data.close(s, d), OFFICIAL), d, EXIT_DELISTED, [])
            # 3. carried, then deferred exits
            for s, p in list(self.pos.items()):
                if p.pending == EXIT_CARRIED:
                    self._try_sell(s, p, d, p.cause or REBALANCE, carry=True)
            for s, p in list(self.pos.items()):
                if p.pending == EXIT_DEFERRED and not data.dq_excluded(s, d):
                    self._try_sell(s, p, d, p.cause or REBALANCE)
            # 4. demerger on the next trading day
            nxt = data.next_trading_day(d)
            demerging = {
                s
                for s in self.pos
                if nxt and any((s, x) in data.demergers for x in _days_between(d, nxt))
            }
            for s in demerging:
                p = self.pos[s]
                if p.pending is None:
                    self._try_sell(s, p, d, EXIT_CORP_ACTION)
            # 5. rebalance
            ruined = ruin is not None
            if d in reb_set:
                forced = d == last_reb
                eq_w = self.cash + sum(
                    p.qty * (self.m.value(s, d)[0] or 0.0) for s, p in self.pos.items()
                )
                if not forced and eq_w < cfg.sizing.ruin_equity and ruin is None:
                    ruin = d
                ruined = ruin is not None
                slot_w = cfg.sizing.slot_fraction * eq_w / cfg.sizing.slots
                targets = [] if forced else list(self.selector(d))
                for s, p in list(self.pos.items()):
                    if forced:
                        p.pending = None
                        self._try_sell(s, p, d, FORCED_END, guard=False)
                    elif s in targets or p.pending is not None:
                        continue
                    elif cfg.universe.dq_defers_exits and data.dq_excluded(s, d):
                        p.pending, p.cause = EXIT_DEFERRED, REBALANCE
                    else:
                        self._try_sell(s, p, d, REBALANCE)
                skipped = []
                if not forced:
                    free = cfg.sizing.slots - len(self.pos)
                    for s in targets:
                        if s in self.pos:
                            continue
                        if free <= 0:
                            break
                        free -= 1
                        if ruined:
                            reason = RUIN
                        elif nxt and any((s, x) in data.demergers for x in _days_between(d, nxt)):
                            reason = CORP_ACTION_NEXT_DAY
                        else:
                            reason = self._buy(s, d, slot_w)
                        if reason:
                            self.counts[reason] += 1
                            skipped.append((s, reason))
                if self.record:
                    reb_rows.append(
                        {
                            "date": d,
                            "book": self.name,
                            "equity_w": eq_w,
                            "slot_w": slot_w,
                            "ruined": ruined,
                            "forced_end": forced,
                            "targets": ";".join(targets),
                            "held_after": ";".join(sorted(self.pos)),
                            "skipped": ";".join(f"{s}:{r}" for s, r in skipped),
                        }
                    )
            # 6. contract note
            tot = (
                round_day([o["charges"] for o in self.orders_today])
                if cfg.execution.round_stt_stamp_to_rupee
                else [sum(o["charges"].values()) for o in self.orders_today]
            )
            for o, t in zip(self.orders_today, tot, strict=True):
                o["cost_r"] = t + o["dp"]
                self.cash -= o["cost_r"] - o["cost_u"]
                p = o["position"]
                if o["side"] == "buy":
                    p.entry_cost_r = o["cost_r"]
                else:
                    trades.append(self._trade(p, o)) if self.record else None
                    self.counts["ROUND_TRIPS"] += 1
                    for f in set(o["flags"]) | {o["cause"]}:
                        self.counts["exit:" + f] += 1
            # 7. invariants, mark-to-market
            if self.check:
                invariants.check(
                    d,
                    d in reb_set,
                    ruined,
                    self.orders_today,
                    divs,
                    cash_start,
                    self.cash,
                    len(self.pos),
                    cfg,
                )
            eq = self._mtm(d)
            if self.record:
                orders += [
                    {k: v for k, v in o.items() if k != "position"} for o in self.orders_today
                ]
                daily.append(
                    {
                        "date": d,
                        "book": self.name,
                        "cash": self.cash,
                        "equity": eq,
                        "positions": len(self.pos),
                        "dividends": divs,
                    }
                )
            if nxt is None or nxt.year != d.year:
                year_end[d.year] = eq
            prev_td = d
        final = self._mtm(last_day)
        year_end[last_day.year] = final
        return LedgerResult(
            self.name, trades, orders, daily, reb_rows, self.counts, ruin, final, year_end
        )

    def _trade(self, p: Position, o: dict) -> dict:
        d = self.data
        gross = o["qty"] * o["fill"] - p.entry_qty * p.entry_fill
        cost_u = p.entry_cost_u + o["cost_u"]
        cost_r = (p.entry_cost_r or p.entry_cost_u) + o["cost_r"]
        flags = o["flags"]
        reason = o["cause"] if o["cause"] != REBALANCE else (flags[-1] if flags else REBALANCE)
        hold = d.day_pos.get(o["day"], 0) - d.day_pos.get(p.entry_day, 0)
        t = {
            "book": self.name,
            "symbol": p.symbol,
            "entry_date": p.entry_day,
            "entry_time": minute_time(self.cfg, p.entry_slot),
            "entry_raw": p.entry_raw,
            "entry_fill": p.entry_fill,
            "entry_qty": p.entry_qty,
            "exit_date": o["day"],
            "exit_time": minute_time(self.cfg, o["slot"]),
            "exit_raw": o["raw"],
            "exit_fill": o["fill"],
            "exit_qty": o["qty"],
            "exit_cause": o["cause"],
            "exit_reason": reason,
            "flags": ";".join(dict.fromkeys(flags)),
            "slot_w": p.slot_w,
            "notional": p.entry_qty * p.entry_fill,
            "gross_pnl": gross,
            "dividends": p.dividends,
            "dp": o["dp"],
            "slippage_paid": p.entry_qty * abs(p.entry_fill - p.entry_raw)
            + o["qty"] * abs(o["fill"] - o["raw"]),
            "costs": cost_u,
            "costs_rounded": cost_r,
            "net_pnl": gross + p.dividends - cost_u,
            "net_pnl_rounded": gross + p.dividends - cost_r,
            "dropped_fraction": p.dropped_fraction,
            "rights_held": p.rights_held,
            "holding_days": hold,
        }
        for leg, ch in (("entry", p.entry_charges), ("exit", o["charges"])):
            for k in CHARGES:
                t[f"{leg}_{k}"] = ch[k]
        return t
