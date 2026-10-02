import shutil
from datetime import date, time

import pytest
import yaml
from pydantic import ValidationError

from orb.config import Config, load_config
from tests.conftest import ROOT


def _copy_config(tmp_path, mutate=None, tick_mutate=None):
    for name in ("default.yaml", "tick_sizes.yaml", "costs.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / name)
    for name, fn in (("default.yaml", mutate), ("tick_sizes.yaml", tick_mutate)):
        if fn:
            raw = yaml.safe_load((tmp_path / name).read_text())
            fn(raw)
            (tmp_path / name).write_text(yaml.safe_dump(raw))
    return tmp_path / "default.yaml"


def test_default_config_loads_with_spec_values(cfg: Config):
    assert cfg.scoring.threshold == 65
    assert cfg.session.or_end == time(9, 29)
    assert cfg.session.hard_exit == time(15, 10)
    assert cfg.portfolio.slot_size == pytest.approx(8000 / 3)
    assert cfg.portfolio.risk_cap == 100
    assert cfg.execution.slippage_pct == 0.0002
    assert cfg.features.atr_period == 14
    assert cfg.run.oos_start == date(2024, 10, 1)
    assert cfg.costs.schedules[0].brokerage_cap == 20


def test_unverified_tables_warn():
    with pytest.warns(UserWarning, match="unverified"):
        load_config(ROOT / "config" / "default.yaml")


def test_hash_is_stable_and_key_order_independent(tmp_path, cfg):
    def reorder(raw):
        raw["scoring"] = dict(reversed(list(raw["scoring"].items())))

    assert load_config(_copy_config(tmp_path, reorder)).hash() == cfg.hash()


def test_hash_covers_tick_table(tmp_path, cfg):
    def bump(raw):
        raw["regimes"][0]["bands"][0]["tick"] = 0.10

    assert load_config(_copy_config(tmp_path, tick_mutate=bump)).hash() != cfg.hash()


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r["scoring"]["breakout"].update(weight=31), "sum to 100"),
        (lambda r: r["session"].update(entry_end="15:20"), "out of order"),
        (lambda r: r["run"].update(oos_start="2017-01-01"), "oos_start"),
        (lambda r: r["portfolio"].update(max_deployment=20000), "leverage"),
        (lambda r: r["scoring"]["or_quality"].update(flat_min=0.05), "breakpoints"),
        (lambda r: r["validation"].update(score_buckets=[60, 75, 85]), "score_buckets"),
        (lambda r: r["scoring"].update(unknown_key=1), "Extra inputs"),
        (lambda r: r["data"].update(daily_history_start="2019-01-01"), "daily_history_start"),
    ],
)
def test_invalid_configs_rejected(tmp_path, mutate, msg):
    with pytest.raises(ValidationError, match=msg):
        load_config(_copy_config(tmp_path, mutate))


def test_config_is_frozen(cfg):
    with pytest.raises(ValidationError):
        cfg.scoring.threshold = 50  # type: ignore[misc]
