"""V1 rule-based scorer: the five factor formulas of the spec, parameters from config."""

from __future__ import annotations

from collections.abc import Mapping

from orb.config import ScoringConfig


def clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def _trapezoid(x: float, lo: float, flat_lo: float, flat_hi: float, hi: float) -> float:
    """0 outside [lo, hi]; linear 0->1 on [lo, flat_lo); 1 on [flat_lo, flat_hi];
    linear 1->0 on (flat_hi, hi]."""
    if flat_lo <= x <= flat_hi:
        return 1.0
    if lo <= x < flat_lo:
        return (x - lo) / (flat_lo - lo)
    if flat_hi < x <= hi:
        return (hi - x) / (hi - flat_hi)
    return 0.0


class RuleScorer:
    name = "rule_v1"

    def __init__(self, cfg: ScoringConfig):
        self.cfg = cfg

    def explain(self, f: Mapping[str, float]) -> dict[str, float]:
        c = self.cfg
        side = f["side"]
        breakout = c.breakout.weight * clip01(f["d"] / c.breakout.full_at_atr)
        rel_volume = c.rel_volume.weight * clip01(f["rv"] - 1.0)
        r = f["r_idx"] * side  # mirror for shorts
        if r > c.market.band:
            market = c.market.weight
        elif r >= -c.market.band:
            market = c.market.neutral_points
        else:
            market = 0.0
        q = c.or_quality
        or_quality = q.weight * _trapezoid(
            f["w"], q.filter_min, q.flat_min, q.flat_max, q.filter_max
        )
        v = c.volatility
        volatility = v.weight * _trapezoid(f["v"], v.ramp_min, v.flat_min, v.flat_max, v.ramp_max)
        return {
            "s_breakout": breakout,
            "s_rel_volume": rel_volume,
            "s_market": market,
            "s_or_quality": or_quality,
            "s_volatility": volatility,
        }

    def score(self, features: Mapping[str, float]) -> float:
        return min(max(sum(self.explain(features).values()), 0.0), 100.0)
