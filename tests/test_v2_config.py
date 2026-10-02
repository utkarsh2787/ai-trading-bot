"""V2 config: loads, its hash matches PREREGISTRATION_V2.md, V1's hash is unchanged."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from orb.cli import prereg_config_hash
from orb.config import load_config
from orb.v2.config import PreregV2, load_config_v2

ROOT = Path(__file__).resolve().parents[1]
V1_HASH = "0822eb04daa9cd48e788a8bd75152743a77a2cf6931e884dfd144e0af62eea0b"


def test_v1_hash_unchanged():
    assert load_config(ROOT / "config" / "default.yaml").hash() == V1_HASH
    assert prereg_config_hash(ROOT / "docs" / "PREREGISTRATION.md") == V1_HASH


def test_v2_hash_matches_preregistration():
    cfg = load_config_v2(ROOT / "config" / "v2.yaml")
    assert cfg.hash() == prereg_config_hash(ROOT / "docs" / "PREREGISTRATION_V2.md")
    assert cfg.hash() != V1_HASH


def test_v2_values():
    c = load_config_v2(ROOT / "config" / "v2.yaml")
    assert c.signal.z_min == 0.25 and c.selection.max_positions == 3
    assert (c.sizing.starting_capital, c.sizing.deploy_fraction, c.sizing.ruin_equity) == (
        10_000,
        0.80,
        5_000,
    )
    assert c.prereg.strategies_tested == 2 and c.prereg.null_p_max == 0.025
    assert c.execution.books["primary"].round_to_tick is False
    assert c.execution.books["v1_model"].pct == 0.0002
    # shared sections are the V1 ones, value for value
    v1 = load_config(ROOT / "config" / "default.yaml")
    assert c.data == v1.data and c.reference == v1.reference
    assert c.ticks == v1.ticks and c.costs == v1.costs


def test_bonferroni_enforced():
    base = dict(version="V2", min_trades=300, min_positive_year_share=0.6, ruin_never_hit=True)
    PreregV2(strategies_tested=2, null_p_max=0.025, **base)
    with pytest.raises(ValidationError):
        PreregV2(strategies_tested=2, null_p_max=0.05, **base)
