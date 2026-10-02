from pathlib import Path

import pytest

from orb.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(ROOT / "config" / "default.yaml")
