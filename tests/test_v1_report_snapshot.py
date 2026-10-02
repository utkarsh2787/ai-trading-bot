"""V1's report.md is unchanged by the V2 work: line for line against a snapshot.

The snapshot (tests/fixtures/v1_golden_report.md) was rendered from the V1
golden day before any V2 report code existed. Only the run id (a timestamp) is
normalised. Regenerate deliberately with ORB_UPDATE_SNAPSHOT=1, never to make a
failing test pass.
"""

import os
import re
from pathlib import Path

import polars as pl

from orb import reports
from orb.engine import Engine, write_run
from orb.scoring import RuleScorer
from orb.sim.ticks import daily_ticks
from tests.test_engine_golden import golden_day

SNAP = Path(__file__).parent / "fixtures" / "v1_golden_report.md"


def render(cfg, tmp_path) -> str:
    m, day = golden_day(cfg)
    ticks = {
        (r["symbol"], r["date"]): r["tick"] for r in daily_ticks(m.daily, cfg.ticks).to_dicts()
    }
    res = Engine(cfg, m.builder(), ticks, RuleScorer(cfg.scoring)).run(
        day, day, ledger=tmp_path / "l.jsonl"
    )
    out = write_run(res, cfg, tmp_path / "runs", {})
    tags = reports.RegimeTags(
        vix=pl.DataFrame(schema={"date": pl.Date, "vix_tercile": pl.String}),
        trend=pl.DataFrame(schema={"date": pl.Date, "trend_day": pl.Boolean}),
        expiry=pl.DataFrame(schema={"date": pl.Date, "is_expiry": pl.Boolean}),
        results=pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date}),
    )
    text = (reports.build_report(out, cfg, tags) / "report.md").read_text()
    return re.sub(r"\d{8}T\d{6}_[0-9a-f]{8}", "<run_id>", text)


def test_v1_report_line_for_line(cfg, tmp_path):
    text = render(cfg, tmp_path)
    if os.environ.get("ORB_UPDATE_SNAPSHOT") == "1":
        SNAP.write_text(text)
    assert text.splitlines() == SNAP.read_text().splitlines()
