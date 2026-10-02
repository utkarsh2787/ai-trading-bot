"""Abstract market-data source. The engine and pipeline depend only on this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import polars as pl


class ProviderError(Exception):
    """A request failed after retries; other symbols may still succeed."""


class FatalProviderError(ProviderError):
    """Auth/permission failure: every further request would fail too."""


class SymbolNotFound(ProviderError):
    """The provider has no instrument for this symbol (e.g. delisted/renamed)."""


class DataProvider(ABC):
    """Bars for a stock or an index (indices are addressed by name, e.g. 'NIFTY 200').

    Both methods return frames conforming to ``orb.data.schema`` for the
    inclusive date range ``[start, end]``.
    """

    name: str

    @abstractmethod
    def minute_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...

    @abstractmethod
    def daily_bars(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...
