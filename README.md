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
export KITE_API_KEY=... KITE_ACCESS_TOKEN=...   # from your own daily login
uv run orb download            # resumable; progress in data/_manifest/downloads.jsonl
uv run orb dq                  # -> data/_dq/issues.parquet, excluded_stock_days.parquet
```

Set `data.provider: local` to import vendor CSV/Parquet files instead.

## Status

1. Gap list + architecture: done
2. Config + data pipeline + DQ checks: done
3. Features + RuleScorer + look-ahead tests: next
4. Backtest engine
5. Signal log + labels
6. Reports
