"""V2's report.md is unchanged by later work: line for line against a snapshot.

Rendered from the V2 golden day before any V3 code existed. Only the run id is
normalised. Regenerate deliberately with ORB_UPDATE_SNAPSHOT=1.
"""

import os
import re
from pathlib import Path

from orb.v2.engine import EngineV2, write_run_v2
from orb.v2.report import build_report_v2
from tests.test_v2_golden import golden_market
from tests.test_v2_report import empty_tags
from tests.v2_market import T, builder

SNAP = Path(__file__).parent / "fixtures" / "v2_golden_report.md"


def test_v2_report_line_for_line(cfg_v2, tmp_path):
    m = golden_market(cfg_v2)
    res = EngineV2(cfg_v2, builder(m), {(s, T): 0.01 for s in m.symbols}, None, {}).run(T, T)
    out = write_run_v2(res, cfg_v2, tmp_path / "runs", {"config_hash": cfg_v2.hash()})
    text = (build_report_v2(out, cfg_v2, empty_tags()) / "report.md").read_text()
    text = re.sub(r"\d{8}T\d{6}_[0-9a-f]{8}", "<run_id>", text)
    if os.environ.get("ORB_UPDATE_SNAPSHOT") == "1":
        SNAP.write_text(text)
    assert text.splitlines() == SNAP.read_text().splitlines()
