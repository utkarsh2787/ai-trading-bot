"""V3 config: loads, hash matches PREREGISTRATION_V3.md; V1 / V2 hashes unchanged."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from orb.cli import prereg_config_hash
from orb.config import load_config
from orb.v2.config import load_config_v2
from orb.v3.config import PreregV3, load_config_v3

ROOT = Path(__file__).resolve().parents[1]


def test_v3_hash_matches_preregistration():
    h = load_config_v3(ROOT / "config" / "v3.yaml").hash()
    assert h == prereg_config_hash(ROOT / "docs" / "PREREGISTRATION_V3.md")


def test_v1_v2_hashes_unchanged():
    assert load_config(ROOT / "config" / "default.yaml").hash() == prereg_config_hash(
        ROOT / "docs" / "PREREGISTRATION.md"
    )
    assert load_config_v2(ROOT / "config" / "v2.yaml").hash() == prereg_config_hash(
        ROOT / "docs" / "PREREGISTRATION_V2.md"
    )


def test_v3_values():
    c = load_config_v3(ROOT / "config" / "v3.yaml")
    assert (c.signal.target_size, c.sizing.slots, c.sizing.slot_fraction) == (2, 2, 0.90)
    assert c.prereg.strategies_tested == 3 and c.prereg.null_p_max == pytest.approx(
        0.0167, abs=1e-4
    )
    assert c.universe.fno_ban_excludes is False
    s = c.costs.schedules[0]
    assert (s.brokerage_pct, s.stt_buy_pct, s.stt_sell_pct, s.stamp_buy_pct) == (
        0.0,
        0.001,
        0.001,
        0.00015,
    )
    assert s.dp_per_scrip_sell_day == 15.93
    assert len(c.costs.unverified) == len(c.costs.verification)  # every field unverified
    v1 = load_config(ROOT / "config" / "default.yaml")
    assert c.data == v1.data and c.reference == v1.reference and c.ticks == v1.ticks
    assert c.costs.schedules[0].exchange_txn_pct == v1.costs.schedules[0].exchange_txn_pct


def test_bonferroni_enforced():
    base = dict(
        version="V3", min_round_trips=300, min_years_beating_null_share=0.6, ruin_never_hit=True
    )
    PreregV3(strategies_tested=3, null_p_max=0.05 / 3, **base)
    with pytest.raises(ValidationError):
        PreregV3(strategies_tested=3, null_p_max=0.025, **base)
