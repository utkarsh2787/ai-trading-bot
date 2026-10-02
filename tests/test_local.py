from datetime import date, datetime

import pytest

from orb.config import LocalConfig
from orb.data.local import LocalProvider
from orb.data.provider import SymbolNotFound


def _cfg(root):
    return LocalConfig(
        root=str(root),
        minute_glob="minute/{symbol}.*",
        daily_glob="daily/{symbol}.*",
        timezone="Asia/Kolkata",
        columns={
            "Datetime": "ts",
            "Date": "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        },
    )


def test_vendor_csv_normalised(tmp_path):
    (tmp_path / "minute").mkdir()
    (tmp_path / "minute" / "INFY.csv").write_text(
        "Datetime,Open,High,Low,Close,Volume\n"
        "2024-03-05 09:16:00,101,102,100,101.5,20\n"
        "2024-03-05 09:15:00,100,101,99,100.5,10\n"
        "2024-03-06 09:15:00,100,101,99,100.5,10\n"
    )
    df = LocalProvider(_cfg(tmp_path)).minute_bars("INFY", date(2024, 3, 5), date(2024, 3, 5))
    assert df["ts"].dtype.time_zone == "Asia/Kolkata"
    assert df["ts"].dt.replace_time_zone(None).to_list() == [
        datetime(2024, 3, 5, 9, 15),
        datetime(2024, 3, 5, 9, 16),
    ]
    assert df["symbol"].to_list() == ["INFY", "INFY"]
    assert df["volume"].to_list() == [10, 20]


def test_vendor_daily_and_missing_symbol(tmp_path):
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "INFY.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n2024-03-05,1,2,0.5,1.5,100\n"
    )
    p = LocalProvider(_cfg(tmp_path))
    assert p.daily_bars("INFY", date(2024, 1, 1), date(2024, 12, 31))["date"].to_list() == [
        date(2024, 3, 5)
    ]
    with pytest.raises(SymbolNotFound):
        p.daily_bars("TCS", date(2024, 1, 1), date(2024, 12, 31))
