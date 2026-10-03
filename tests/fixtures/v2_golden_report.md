survivorship gap: 0.00% of eligible stock-days have no 1-min data (0/4)
[FAIL] a. >= 300 taken trades: 3
[FAIL] b. net P&L (rupee-rounded) > 0 under primary slippage and >= 0 under v1_model stress: -151.72 / -156.19
[FAIL] c. beats random-direction null at p < 0.025 (Bonferroni, 2 strategies): p = 0.7545
[FAIL] d. net positive in >= 60% of calendar years and the ruin stop never hit: 0/1 (0%); ruin never
[FAIL] e. (informational) net return per trade rises across |z| terciles of qualifying stocks: -0.00126 < -0.03199 < 0.00664
IN-SAMPLE GATE (V2, a-d): FAIL -> OOS run NOT allowed
capital model: equity tracked per book from Rs 10,000; Slot_d = 80% x equity_d / 3; no new trades once equity_d < Rs 5,000
ruin dates: primary never, primary_2x never, v1_model never, index_variant never

# V2 backtest report: run <run_id>

- period: 2024-06-28 .. 2024-06-28 (in-sample)
- config hash: `ac6b5b549c9716588246fa93f31d5c4f8093467709db693e82ff41b9246e94c7`
- data version: `None` (vendor snapshot None)
- primary book: `primary` slippage, rupee-rounded costs; stress: `v1_model`
- code: no provenance recorded

## Headline metrics (every book)
| book | trades | hit_rate | gross_pnl | costs | costs_rounded | net_pnl | net_pnl_rounded | slippage_paid | avg_ret_net | max_drawdown | final_equity | ruin_date | days_traded | hard_exit | exit_substituted | exit_locked | exit_locked_unfilled |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| primary | 3 | 0.3333 | -143.48 | 8.47 | 8.24 | -151.95 | -151.72 | 1.58 | -0.019113 | 151.72 | 9848.28 |  | 1 | 1 | 1 | 1 | 0 |
| primary_2x | 3 | 0.3333 | -145.06 | 8.47 | 8.24 | -153.53 | -153.3 | 3.16 | -0.019314 | 153.3 | 9846.7 |  | 1 | 1 | 1 | 1 | 0 |
| v1_model | 3 | 0.3333 | -147.95 | 8.47 | 8.24 | -156.42 | -156.19 | 6.05 | -0.01968 | 156.19 | 9843.81 |  | 1 | 1 | 1 | 1 | 0 |
| index_variant | 0 |  | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |  | 0.0 | 10000.0 |  | 0 | 0 | 0 | 0 | 0 |

## Skips and flags
| book | selected | locked_circuit | entry_missing | qty_zero | no_tick | ruin | p0945_substituted_signals |
|---|---|---|---|---|---|---|---|
| primary | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| primary_2x | 3 | 0 | 0 | 0 | 0 | 0 | 0 |
| v1_model | 3 | 0 | 0 | 0 | 0 | 0 | 0 |

## Nulls
### Primary: random direction per trade (criterion c)
_Only the raw move flips; slippage and the actual unrounded costs stay costs on both sides._
```
{
 "trades": 3,
 "draws": 2000,
 "seed": 20180101,
 "usable": 3,
 "actual_total": -151.95,
 "random_mean": -8.89,
 "random_p05": -193.55,
 "random_p95": 173.45,
 "p_value": 0.7545,
 "mean_diff_per_trade": -47.3,
 "mean_diff_ci95": [
  -194.4,
  27.7333
 ]
}
```
### Secondary: always-long baseline (same trades, qty and costs)
```
{
 "trades": 3,
 "net_actual_unrounded": -151.95,
 "net_always_long_unrounded": 139.65,
 "hit_rate_always_long": 0.6667
}
```
### Secondary: index-level variant (own equity path)
```
{
 "trades": 0,
 "net_rounded": 0.0,
 "ruin_date": null,
 "null": {
  "trades": 0,
  "usable": 0
 }
}
```

## Secondary diagnostics (excluded from pass/fail)
_Trades removed from the existing books; the equity path is not re-run._
| scenario | trades | net_rounded_primary | net_rounded_v1_model | null_p |
|---|---|---|---|---|
| all trades (reference = criteria a-c) | 3 | -151.72 | -156.19 | 0.7545 |
| drift stock-days excluded | 3 | -151.72 | -156.19 | 0.7545 |
| from 2020-01-01 (lower survivorship gap) | 3 | -151.72 | -156.19 | 0.7545 |
| results-day trades removed | 3 | -151.72 | -156.19 | 0.7545 |

share of primary-book trades on results days: 0/3 (0.0%)

### side
| side | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| long | 2 | 0.5 | 17.48 | -20.21 | 2.86 | 5.6 | -2.74 | -2.59 | -1.37 | 1.04 | 39.5 | 0.0006 | -0.0005 | 0.865 |
| short | 1 | 0.0 |  | -149.21 | -146.34 | 2.87 | -149.21 | -149.13 | -149.21 | 0.54 | 42.0 | -0.0553 | -0.0563 |  |

### vix_tercile
| vix_tercile | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| untagged | 3 | 0.3333 | 17.48 | -84.71 | -143.48 | 8.47 | -151.95 | -151.72 | -50.65 | 1.58 | 40.3 | -0.018 | -0.0191 | 0.206 |

### trend_vs_range
| day_type | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| untagged | 3 | 0.3333 | 17.48 | -84.71 | -143.48 | 8.47 | -151.95 | -151.72 | -50.65 | 1.58 | 40.3 | -0.018 | -0.0191 | 0.206 |

### results_day
| results_day | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 17.48 | -84.71 | -143.48 | 8.47 | -151.95 | -151.72 | -50.65 | 1.58 | 40.3 | -0.018 | -0.0191 | 0.206 |

### budget_day
| budget_day | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 17.48 | -84.71 | -143.48 | 8.47 | -151.95 | -151.72 | -50.65 | 1.58 | 40.3 | -0.018 | -0.0191 | 0.206 |

### expiry
| is_expiry | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_ret_gross | avg_ret_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 17.48 | -84.71 | -143.48 | 8.47 | -151.95 | -151.72 | -50.65 | 1.58 | 40.3 | -0.018 | -0.0191 | 0.206 |

## Criterion e: label net return by |z| tercile (informational)
| tercile | lo | hi | labels | avg_ret_net | avg_ret_gross | avg_net_pnl | hit_rate |
|---|---|---|---|---|---|---|---|
| 1 | 0.4999999999999968 | 0.4999999999999968 | 1 | -0.0012603039696031419 | -0.0001999800019999023 | -3.277118000000266 | 0.0 |
| 2 | 0.6000000000000005 | 0.699999999999997 | 2 | -0.031989702816532864 | -0.030919748605599668 | -84.71228875490004 | 0.0 |
| 3 | 0.8000000000000007 | 0.8000000000000007 | 1 | 0.006641362284359265 | 0.0077067483450252066 | 17.47647919680003 | 1.0 |

