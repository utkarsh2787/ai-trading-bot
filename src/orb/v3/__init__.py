"""V3: weekly short-term reversal, long-only delivery (CNC), Nifty 200.

The 2 worst past-week performers at 15:00 on the last trading day of each week
are held for the following week. Fixed research defaults in ``config/v3.yaml``;
pre-registration in ``docs/PREREGISTRATION_V3.md``. V1/V2 code and configs are
untouched; V3 reuses the shared data, reference, DQ, tick, fill and guard code.
"""
