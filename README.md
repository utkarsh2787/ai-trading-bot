# ai-trading-bot: ORB research backtester

A research-only backtester for an intraday Opening Range Breakout strategy on the
NSE Nifty 200. It places no live orders and uses no broker order APIs.

- `config/`: every research parameter (fixed defaults, not optimised)
- `docs/DECISIONS.md`: how each spec ambiguity was resolved
- `docs/DATA_SOURCES.md`: data availability and reference-file formats

## Setup

```bash
uv sync --extra kite          # Python 3.12 + deps (kiteconnect optional)
uv run pytest
```

## Data pipeline

```bash
uv run orb ref all             # NSE/niftyindices reference data + raw bhavcopy (resumable)
export KITE_API_KEY=... KITE_ACCESS_TOKEN=...   # from your own daily login
uv run orb download            # Kite bars, as delivered (adjusted) -> data/vendor/kite
uv run orb build-raw           # de-adjust against the bhavcopy -> data/raw
uv run orb dq                  # checks + exclusion report -> data/_dq/
uv run orb scan                # first-breakout candidates (in-sample; OOS needs --oos)
```

Set `data.provider: local` to import vendor CSV/Parquet files instead. See
`docs/DATA_SOURCES.md` for what must be downloaded by hand.

## Status

1. Gap list + architecture: done
2. Config + data pipeline + DQ checks: done
3. Features + RuleScorer + look-ahead tests: done
4. Backtest engine: next
5. Signal log + labels
6. Reports
