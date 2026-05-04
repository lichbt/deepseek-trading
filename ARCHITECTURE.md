# Architecture & Design

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                 Trading Strategy Pipeline (Oanda)               │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────────┐          ┌──────────────────┐             │
│  │   Researcher     │          │   Validator      │             │
│  │   Agent (LLM)    │─────────>│   (backtest)     │             │
│  │                  │          │                  │             │
│  │ Generates JSON   │          │ 4-gate filter:   │             │
│  │ (code + grid)    │          │ 1. Fingerprint   │             │
│  └──────────────────┘          │ 2. In-sample     │             │
│                                │ 3. Walk-forward  │             │
│  Constraints:                  │ 4. Hold-out      │             │
│  • Pre-2020 data only          │                  │             │
│  • Economic rationale          │ → SQL: results   │             │
│  • Max 4 params, ≤200 combos   └──────────────────┘             │
│  • Deterministic code          │                                │
│                                │                                │
│                                │  ┌──────────────────┐         │
│                                └─>│   Live Tester    │         │
│                                   │  (paper trade)   │         │
│                                   │                  │         │
│                                   │ • Poll Oanda API │         │
│                                   │ • Place orders   │         │
│                                   │ • Track P&L      │         │
│                                   │ • Update metrics │         │
│                                   └──────────────────┘         │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      Data Layer                            │ │
│  ├────────────────────────────────────────────────────────────┤ │
│  │                                                            │ │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │ │
│  │  │ data_fetcher │  │pipeline_utils│  │  pipeline.db │    │ │
│  │  │   (Oanda)    │  │   (core)     │  │   (SQLite)   │    │ │
│  │  │              │  │              │  │              │    │ │
│  │  │• get_candles │  │• GT-Score    │  │ strategies   │    │ │
│  │  │• pagination  │  │• grid_search │  │ validation   │    │ │
│  │  │• error hdl   │  │• walk_fwd    │  │ live_status  │    │ │
│  │  │              │  │• DB ops      │  │              │    │ │
│  │  └──────────────┘  └──────────────┘  └──────────────┘    │ │
│  │                                                            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Hierarchy

### Level 1: Core Utilities (`pipeline_utils.py`)
Foundation layer with all reusable logic.

**Metrics**:
- `compute_gt_score(returns)` → float
- `compute_strategy_returns(data, signals)` → Series

**Optimization**:
- `grid_search(data, strategy_func, param_grid)` → (best_params, score)
- `walk_forward(data, strategy_func, param_grid, n_windows)` → {combined_score, per_window, min, oos_returns}
- `evaluate_on_data(data, strategy_func, params)` → float

**Deduplication**:
- `compute_strategy_fingerprint(code, param_grid)` → str (SHA256)

**Database**:
- `init_db()` → None
- `check_idea_is_new(fingerprint)` → {new: bool, status?: str}
- `insert_strategy(id, fingerprint, code, param_grid, rationale)` → None
- `record_validation(strategy_id, best_params, is_score, wf_score, ho_score, final_status)` → None
- `start_live_trading(strategy_id)` → None
- `update_live_metrics(strategy_id, equity_curve, gt_score)` → None
- `get_passed_strategies()` → List[Dict]
- `get_strategy_by_id(strategy_id)` → Dict

---

### Level 2: Data Access (`data_fetcher.py`)
Encapsulates Oanda v20 API integration.

**Public Interface**:
- `get_candles(instrument, granularity, start, end, count)` → DataFrame
- `get_candles_date_range(instrument, start_date, end_date, granularity)` → DataFrame

**Features**:
- Automatic pagination for large date ranges (Oanda limits 5000 per request)
- Mid-price extraction (bid/ask average)
- Error handling and retry logic
- ISO timestamp parsing

---

### Level 3: Workflow Scripts

#### `validator.py` – Backtesting Entry Point
Validates strategy candidates through 4 gates.

**Input**: JSON file
```json
{
  "strategy_id": "...",
  "code": "def generate_signals(df, params): ...",
  "param_grid": {...},
  "rationale": "..."
}
```

**Output**: Database updates + console "PASS" or "FAIL: <reason>"

**Workflow**:
1. Load & fingerprint check
2. Insert as 'proposed'
3. Load strategy function
4. Fetch dev data (2015-2019)
5. Grid search on dev (IS threshold: > 0.5)
6. Fetch full data (2015-2023 excl. hold-out)
7. Walk-forward (WF thresholds: > 1.0 combined, > 0.3 min)
8. Fetch hold-out (2024+)
9. Hold-out eval (decay < 30%)
10. Record results, set status

**Exit Codes**:
- 0 = PASS
- 1 = FAIL

---

#### `live_test.py` – Paper Trading Entry Point
Deploys passed strategies to Oanda practice account.

**Input**: strategy_id (command-line arg)

**Output**: Continuous polling loop, database updates, console logs

**Workflow**:
1. Load strategy from DB (status must be 'passed')
2. Fetch strategy function and best parameters
3. Initialize live_status entry
4. Poll Oanda API every 60 seconds:
   - Fetch last 500 daily candles
   - Generate signals with best params
   - Place/close market orders if signal changes
   - Track daily P&L
5. Update metrics daily:
   - Compute rolling GT-Score (last 30 days)
   - Update equity curve
   - Save to DB

**Graceful shutdown**: Ctrl+C closes any open positions and exits

---

### Level 4: Orchestration

#### Researcher Agent (`.opencode/agents/researcher.md`)
LLM-driven strategy generation.

**Input**: User request (optionally with domain parameter)
```
Researcher: Generate mean reversion strategy for EUR_USD
```

**Output**: Valid JSON with strategy candidate
```json
{
  "strategy_id": "...",
  "code": "...",
  "param_grid": {...},
  "rationale": "..."
}
```

**Constraints Enforced**:
- Pre-2020 knowledge only
- Max 5 conditions per strategy
- Max 4 parameters, ≤ 200 grid combos
- Deterministic code (pandas/numpy/ta only)
- No look-ahead
- Self-critique before output

---

## Data Flow

### Strategy Lifecycle

```
┌─────────────────────────────────────┐
│  Researcher Agent                    │
│  Generates JSON (code + grid)        │
└──────────┬──────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  validator.py                        │
│  • Fingerprint check                 │
│  • Grid search (IS)                  │
│  • Walk-forward (OOS)                │
│  • Hold-out test                     │
└──────────┬──────────────────────────┘
           │
        ┌──┴──┐
        │     │
      PASS  FAIL
        │     │
        ▼     ▼
      passed  research_failed
      │       walk_forward_failed
      │       holdout_failed
      │
      ▼
┌─────────────────────────────────────┐
│  Database: strategies + validation   │
│  Status = 'passed'                   │
└──────────┬──────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  live_test.py                        │
│  • Poll Oanda API                    │
│  • Place orders                      │
│  • Track equity curve                │
└──────────┬──────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  Database: live_status               │
│  Status = 'paper_trading'            │
│  (→ eventually 'live' or 'retired')  │
└─────────────────────────────────────┘
```

---

## Database Schema

### Table: strategies
Central record for all strategy candidates.

| Column | Type | Constraint | Purpose |
|--------|------|------------|---------|
| id | TEXT | PRIMARY KEY | Unique identifier |
| fingerprint | TEXT | UNIQUE, NOT NULL | Deduplication (SHA256 of code + grid) |
| code | TEXT | NOT NULL | Python source code |
| param_grid | TEXT | NOT NULL | JSON parameter dictionary |
| rationale | TEXT | | Economic hypothesis |
| status | TEXT | DEFAULT 'proposed' | Lifecycle state |
| created_at | TEXT | | ISO timestamp |

**Status Values**:
- `proposed`: Inserted but not yet validated
- `research_failed`: In-sample GT-Score < 0.5
- `walk_forward_failed`: Walk-forward GT-Score < 1.0 or min window < 0.3
- `holdout_failed`: Hold-out decay > 30%
- `passed`: All gates passed, ready for paper trading
- `paper_trading`: Currently live trading on practice
- `live`: Promoted to real money (not auto-set by system)
- `retired`: Permanently disabled

---

### Table: validation_results
Backtesting results for each strategy.

| Column | Type | Constraint | Purpose |
|--------|------|------------|---------|
| strategy_id | TEXT | PRIMARY KEY, FK | References strategies.id |
| best_params | TEXT | | JSON of optimal parameters |
| is_gt_score | REAL | | In-sample GT-Score |
| walk_forward_gt_score | REAL | | Combined walk-forward GT-Score |
| holdout_gt_score | REAL | | Hold-out period GT-Score |
| final_status | TEXT | NOT NULL | 'pass' or 'fail: <reason>' |
| tested_at | TEXT | NOT NULL | ISO timestamp |

---

### Table: live_status
Paper trading progress for each deployed strategy.

| Column | Type | Constraint | Purpose |
|--------|------|------------|---------|
| strategy_id | TEXT | PRIMARY KEY, FK | References strategies.id |
| start_date | TEXT | | When paper trading began |
| equity_curve | TEXT | | JSON list of {date, equity} |
| current_gt_score | REAL | | Rolling GT-Score (last 30 days) |
| last_updated | TEXT | | ISO timestamp |

---

## GT-Score Formula

Combines three risk-adjusted metrics for robust evaluation.

### Components

1. **Sharpe Ratio**: Return per unit of risk
   ```
   Sharpe = E[R] / σ(R)
   E[R] = annualized mean return
   σ(R) = annualized volatility
   ```

2. **Sortino Ratio**: Return per unit of downside risk
   ```
   Sortino = E[R] / σ_down(R)
   σ_down(R) = std dev of negative returns only
   ```

3. **Win Rate**: Consistency of positive periods
   ```
   WinRate = P(R > 0)
   ```

### Final Score
```
GT-Score = (Sharpe + 2*Sortino + 2*(WinRate - 0.5)) / 3

Range: Typically 0.3 to 3.0
Higher = Better
```

### Thresholds
| Gate | Threshold | Rationale |
|------|-----------|-----------|
| In-Sample | > 0.5 | Basic profitability sanity check |
| Walk-Forward | > 1.0 | Reasonable OOS performance |
| Min Window | > 0.3 | No single regime collapses |
| Hold-Out | > 0.7 × WF Score | Allow 30% OOS decay (expected) |

---

## Walk-Forward Implementation

Multi-window backtesting to prevent look-ahead bias.

### Algorithm

```
Input: full_data[t=0...T], param_grid, n_windows, train_len, test_len

for window i in 0..n_windows-1:
    train_start = i * stride
    train_end = train_start + train_len
    test_start = train_end
    test_end = test_start + test_len
    
    if test_end > T:
        break
    
    # Window 1: Train on [t_i1, t_i2], test on [t_i3, t_i4]
    train_data = full_data[train_start:train_end]
    test_data = full_data[test_start:test_end]
    
    # Grid search on train (in-sample optimization)
    best_params, is_score = grid_search(train_data, strategy_func, param_grid)
    
    # Evaluate on test (out-of-sample, no re-optimization)
    oos_signals = strategy_func(test_data, best_params)
    oos_returns = compute_returns(test_data, oos_signals)
    oos_score = compute_gt_score(oos_returns)
    
    per_window_scores.append(oos_score)
    all_oos_returns.append(oos_returns)

# Combine results
combined_score = compute_gt_score(concat(all_oos_returns))
min_score = min(per_window_scores)

Return: {
    'combined_gt_score': combined_score,
    'per_window_gt_scores': per_window_scores,
    'min_window_score': min_score,
    'all_oos_returns': all_oos_returns
}
```

**Key Properties**:
- No data leakage: test data never touches training process
- Multiple regimes: tests across 4+ market periods
- Robust parameters: must work on unseen data
- Conservative thresholds: min window prevents "one lucky window"

---

## Error Handling

### Validator Script
- Fingerprint collision → Exit with message, no DB update
- Code error (e.g., syntax) → Status = 'research_failed', exit 1
- Data fetch error → Status = 'research_failed', exit 1
- Grid search exception → Status = 'research_failed', exit 1
- Walk-forward exception → Status = 'research_failed', exit 1
- Hold-out exception → Status = 'research_failed', exit 1

### Live Trader
- Oanda connection error → Log warning, retry next poll
- Signal generation error → Log warning, hold position
- Order placement error → Log warning, retry next opportunity
- Metrics update error → Log warning, continue trading

---

## Performance Considerations

### Grid Search
- **Complexity**: O(C × N) where C = combinations, N = rows
- **Optimization**: Vectorized pandas operations, no loops
- **Limit**: Keep total combos ≤ 200 (e.g., 4 params × 5–10 values each)

### Walk-Forward
- **Complexity**: O(windows × (C × N_train + N_test))
- **Trade-off**: More windows = more robust but slower
- **Default**: 5 windows over 4+ years of data

### Data Fetching
- **Pagination**: Automatic for large ranges (Oanda: 5000 candles/request)
- **Caching**: None (fresh data each run, intentional for live trading)
- **Rate Limit**: Oanda practice has no strict rate limit

### Live Trading
- **Poll Frequency**: 60 seconds for daily strategies
- **Position Tracking**: Simple long/short/-/0 state machine
- **Metrics Update**: Daily, rolling 30-day window

---

## Extensibility

### Adding Custom Metrics
Modify `compute_gt_score()` in `pipeline_utils.py`:
```python
def compute_gt_score(returns: pd.Series) -> float:
    # Add custom logic here
    return custom_score
```

All validation automatically uses the new metric (grid search, walk-forward, etc.).

### Adding New Data Sources
Extend `data_fetcher.py`:
```python
def get_candles_alternative(instrument, start, end):
    # Fetch from alternative API
    return df
```

Update `validator.py` to use alternative fetcher for specific instruments.

### Adding Live Order Types
Extend `live_test.py` order execution:
```python
def _execute_order(self, units, comment, order_type='MARKET', ...):
    # Support LIMIT, STOP, etc.
```

### Custom Strategy Constraints
Add pre-submission checks in Researcher agent:
```
## Additional Guards
- Code must use only fast (< O(n²)) operations
- Max lookback period: 252 (1 trading year)
```

---

## Security Notes

- **Credentials**: Store in environment variables, never in code
- **Database**: SQLite (local), no network exposure
- **Validation**: Fingerprints prevent submission of identical strategies
- **Order Size**: Hardcoded to 1000 units (micro lot) on practice account
- **No Real Money**: Live trader uses practice account only; promotion to real requires manual override

---

## Testing Checklist

Before deploying to production:

- [ ] `setup_verify.py` passes all checks
- [ ] Test strategy validates successfully
- [ ] Best params are reasonable (no extreme values)
- [ ] Live trader starts without errors
- [ ] Orders are placed on practice account only
- [ ] Equity curve updates daily
- [ ] Database queries return expected results
- [ ] Ctrl+C gracefully closes positions

---

## References

- **GT-Score**: Alexander Sheppert's framework for strategy evaluation
- **Walk-Forward**: De Prado, López de Prado, M.L. (2018). "Advances in Financial Machine Learning"
- **Oanda API**: https://developer.oanda.com/rest-live-v20/
- **TA-Lib**: Technical Analysis Library (RSI, MACD, Bollinger Bands, etc.)

