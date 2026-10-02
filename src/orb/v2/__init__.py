"""V2: intraday time-series momentum (Gao, Han, Li, Zhou 2018, single stocks).

The return from the previous close to 09:45 predicts the late-session direction.
Fixed research defaults in ``config/v2.yaml``; pre-registration in
``docs/PREREGISTRATION_V2.md``. V1 code and config are untouched; V2 reuses the
shared data, reference, DQ, tick and cost modules.
"""
