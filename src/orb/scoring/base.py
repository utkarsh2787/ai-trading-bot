"""Scorer interface. The engine depends only on this protocol.

``features`` is the flat feature snapshot taken at the signal candle close
(see ``orb.signals.FEATURE_KEYS``); ``side`` is +1 (long) / -1 (short).
A scorer may additionally implement ``explain(features) -> dict[str, float]``
returning named components; it is used for logging only, never for decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class Scorer(Protocol):
    name: str

    def score(self, features: Mapping[str, float]) -> float:
        """Signal quality in [0, 100]."""
        ...
