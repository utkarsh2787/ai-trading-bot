from pathlib import Path

import pytest

from orb.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(ROOT / "config" / "default.yaml")


@pytest.fixture(scope="session")
def cfg_v2():
    from orb.v2.config import load_config_v2

    return load_config_v2(ROOT / "config" / "v2.yaml")


@pytest.fixture(scope="session")
def cfg_v3():
    from orb.v3.config import load_config_v3

    return load_config_v3(ROOT / "config" / "v3.yaml")
