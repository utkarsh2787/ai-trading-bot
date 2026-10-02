# Data sources and reference-file formats

## What Kite Connect can and cannot provide

| Need | Kite? | Source / action |
|---|---|---|
| 1-min stock OHLCV | Yes (60-day windows, ~3 req/s) | `orb download`. **Survivorship gap:** delisted/renamed symbols are absent from the current instruments dump and are reported as `unresolved`. Fill them from a vendor (TrueData, GDFL, Accelpix, ...) via the local provider. |
| 1-min NIFTY 200 / NIFTY 50 / INDIA VIX | Index tokens exist; verify how far back minute history goes | Kite, vendor fallback |
| Daily OHLC | Yes | Kite; cross-check against NSE CM bhavcopy. Adjustment is done by `orb.data.adjust` from our own corporate-actions file, so we never rely on vendor adjustment. |
| Point-in-time Nifty 200 constituents | **No** | niftyindices.com index-reconstitution press releases (semi-annual + ad-hoc replacements) |
| F&O ban list | **No** | NSE archives: `fo_secban_DDMMYYYY.csv` per day |
| Corporate actions | **No** | NSE corporate-actions export (ex-date, purpose) |
| Expiry calendar | Current contracts only | NSE F&O bhavcopy history |
| Results dates | **No** | NSE/BSE board-meeting announcements |

The Kite access token comes from your own daily login (`KITE_API_KEY`,
`KITE_ACCESS_TOKEN` env vars). The code never automates login and never calls an
order API.

## Reference files (`data/ref/`, CSV or Parquet, dates `YYYY-MM-DD`)

| File | Required | Columns | Notes |
|---|---|---|---|
| `nifty200_membership.csv` | yes | `date,symbol` **or** `symbol,valid_from,valid_to` | Snapshot form: a symbol in snapshot d_i is a member until the day before the next snapshot. `valid_to` inclusive; empty = still a member. Overlaps rejected. |
| `fo_ban.csv` | yes | `date,symbol` | |
| `corporate_actions.csv` | yes | `symbol,ex_date,action_type,price_factor` | `action_type` ∈ split, bonus, rights, demerger, merger, dividend, other. `price_factor` = multiplier for pre-ex-date prices (1:5 split → 0.2, 1:1 bonus → 0.5); required for split/bonus/rights/demerger. |
| `special_sessions.csv` | yes | `date,session_type` | muhurat, mock, dr, special, other. Excluded. |
| `calendar_exceptions.csv` | no | `date,reason` | Halts / abnormal sessions. Excluded. |
| `budget_days.csv` | no | `date` | Tagged, not excluded. |
| `symbol_map.csv` | no | `old_symbol,new_symbol,effective_date` | |
| `expiries.csv` | no | `date,expiry_type` | `index_weekly`, `stock_monthly` |
| `results_dates.csv` | no | `symbol,date` | |
