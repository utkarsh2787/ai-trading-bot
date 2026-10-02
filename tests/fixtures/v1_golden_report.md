survivorship gap: 0.00% of eligible stock-days have no 1-min data (0/4)
[FAIL] a. >= 300 taken trades: 3
[FAIL] b. net P&L (rupee-rounded) > 0 at 1x and >= 0 at 2x slippage: -93.14 / -97.58
[FAIL] c. beats random-direction null at p < 0.05: p = 0.8740
[FAIL] d. net positive in >= 60% of calendar years: 0/1 (0%)
[FAIL] e. score useful: net R per trade rises across 65-74, 75-84, 85+ (else V1 = plain ORB): n/a < 0.304 < -1.030
IN-SAMPLE GATE (V1, a-d): FAIL -> OOS run NOT allowed; score not shown useful -> evaluate V1 as plain ORB
capital model: Rs 10,000 at the start of EVERY day (resets daily; sizing ignores cumulative P&L; no compounding, no ruin stop), so max drawdown can exceed capital
account ruin date (primary book, first day cumulative rupee-rounded net <= -Rs 10,000): never (min cumulative -93.14, final -93.14)

# ORB backtest report: run <run_id>

- period: 2024-06-28 .. 2024-06-28 (in-sample)
- config hash: `None`
- git: `None`
- data version: `None` (vendor snapshot None)
- primary book: default entry-candle rule, 1x slippage; gross and net shown separately
- code: no provenance recorded

## Summary (all books)
| variant | slippage_mult | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff | days_traded | max_drawdown | profit_factor | stops | hard_exits | exits_substituted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| conservative | 1.0 | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 | 1 | 93.21 | 0.093 | 2 | 1 | 0 |
| conservative | 2.0 | 3 | 0.3333 | 8.24 | -52.95 | -89.8 | 7.85 | -97.65 | -97.58 | -32.55 | 9.4 | 208.3 | -0.7657 | -0.838 | 0.156 | 1 | 97.65 | 0.078 | 2 | 1 | 0 |
| conservative | 3.0 | 3 | 0.3333 | 6.92 | -54.51 | -94.24 | 7.85 | -102.09 | -102.02 | -34.03 | 13.84 | 208.3 | -0.7905 | -0.8613 | 0.127 | 1 | 102.09 | 0.064 | 2 | 1 | 0 |
| default | 1.0 | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 | 1 | 93.21 | 0.093 | 2 | 1 | 0 |
| default | 2.0 | 3 | 0.3333 | 8.24 | -52.95 | -89.8 | 7.85 | -97.65 | -97.58 | -32.55 | 9.4 | 208.3 | -0.7657 | -0.838 | 0.156 | 1 | 97.65 | 0.078 | 2 | 1 | 0 |
| default | 3.0 | 3 | 0.3333 | 6.92 | -54.51 | -94.24 | 7.85 | -102.09 | -102.02 | -34.03 | 13.84 | 208.3 | -0.7905 | -0.8613 | 0.127 | 1 | 102.09 | 0.064 | 2 | 1 | 0 |

## Regimes (primary book)
### side
| side | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| long | 2 | 0.0 |  | -51.39 | -97.24 | 5.53 | -102.77 | -102.72 | -51.39 | 3.64 | 150.0 | -1.2986 | -1.3725 |  |
| short | 1 | 1.0 | 9.56 |  | 11.88 | 2.32 | 9.56 | 9.58 | 9.56 | 1.32 | 325.0 | 0.3776 | 0.3039 |  |

### vix_tercile
| vix_tercile | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| untagged | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 |

### trend_vs_range
| day_type | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| untagged | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 |

### results_day
| results_day | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 |

### budget_day
| budget_day | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 |

### expiry
| is_expiry | trades | hit_rate | avg_win | avg_loss | gross_pnl | costs | net_pnl | net_pnl_rounded | avg_net | slippage_paid | avg_hold_min | avg_r_gross | avg_r_net | payoff |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| False | 3 | 0.3333 | 9.56 | -51.39 | -85.36 | 7.85 | -93.21 | -93.14 | -31.07 | 4.96 | 208.3 | -0.7399 | -0.8137 | 0.186 |

## Random-direction null (primary book): criterion c
```
{
 "trades": 3,
 "draws": 2000,
 "seed": 20180101,
 "usable": 3,
 "actual_total": -93.21,
 "random_mean": -36.37,
 "random_p05": -119.61,
 "random_p95": 46.67,
 "p_value": 0.874,
 "mean_diff_per_trade": -18.9133,
 "mean_diff_ci95": [
  -66.4733,
  17.6
 ],
 "same_exit_time_share": 0.3333
}
```
### Secondary diagnostic: strict sign flip at the actual exit time
_Excluded from pass/fail (pre-registration amendment 2026-10-02)._
```
{
 "usable": 3,
 "actual_total": -93.21,
 "random_mean": -7.39,
 "random_p05": -116.97,
 "random_p95": 101.27,
 "p_value": 0.8785,
 "mean_diff_per_trade": -28.4533,
 "mean_diff_ci95": [
  -90.3067,
  15.84
 ]
}
```

## Score validity: score_buckets
| score_bucket | signals | sized | avg_net_pnl | net_pnl | hit_rate | avg_r_net | avg_r_per_share_gross |
|---|---|---|---|---|---|---|---|
| 75-84 | 1 | 1 | 9.56 | 9.56 | 1.0 | 0.3039 | 0.3776 |
| 85+ | 3 | 3 | -35.87 | -107.62 | 0.0 | -1.0302 | -0.9151 |

## Score validity: factor_quintiles
_(none)_

## Secondary diagnostics (excluded from pass/fail)
_Pre-registration amendment 2026-10-02. Trades are removed from the existing book (freed slots are not re-allocated); default variant, rupee-rounded net._
| scenario | trades | net_rounded_1x | net_rounded_2x | null_p | null_trades |
|---|---|---|---|---|---|
| all trades (reference = criteria a-c) | 3 | -93.14 | -97.58 | 0.874 | 3 |
| drift stock-days excluded | 3 | -93.14 | -97.58 | 0.874 | 3 |
| from 2020-01-01 (lower survivorship gap) | 3 | -93.14 | -97.58 | 0.874 | 3 |
