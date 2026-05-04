# Trading Strategy Research & Validation Pipeline (Oanda + GT-Score)

A disciplined, automated framework for researching, backtesting, and live paper-trading FX and commodity strategies on Oanda's practice account. Built around a single metric (GT-Score) and strict design rules to prevent repeated work and overfitting.

## Quick Start

### 1. Prerequisites
- Python 3.8+
- Oanda practice account (free)
- Oanda API token and account ID

### 2. Environment Setup

```bash
# Clone or navigate to workspace
cd /Users/lich/deepseek-trading

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install pandas numpy requests ta-lib

# Set environment variables (add to .env or terminal)
export OANDA_ACCOUNT_ID="your_account_id"
export OANDA_API_TOKEN="your_api_token"
```

### 3. Initialize Database
```bash
python3 -c "from pipeline_utils import init_db; init_db()"
```

This creates `pipeline.db` with three tables: `strategies`, `validation_results`, `live_status`.

---

## Workflow

### Phase 1: Strategy Research
Use the **Researcher Agent** (OpenCode subagent in `.opencode/agents/researcher.md`) to generate strategy candidates:

```bash
# Invoke OpenCode Researcher (in chat or via API)
Researcher: Generate a mean reversion strategy for EUR_USD
```

**Output**: JSON file with strategy candidate
```json
{
  "strategy_id": "mean_rev_eur_v1",
  "code": "def generate_signals(df, params):\n    ...",
  "param_grid": {"lookback": [10, 20, 30], "threshold": [1.5, 2.0, 2.5]},
  "rationale": "Mean reversion exploits overbought/oversold extremes."
}
```

### Phase 2: Validation (Backtesting)
Run the validator to backtest the candidate through all gates:

```bash
python validator.py strategy_candidate.json
```

**Validator workflow**:
1. Check fingerprint (SHA256 of code + param_grid) for duplicates
2. Grid search on dev data (2015-01-01 to 2019-12-31)
   - Threshold: in-sample GT-Score **> 0.5**
3. Walk-forward on full historical data (5 windows, no look-ahead)
   - Threshold: combined GT-Score **> 1.0**
   - Threshold: minimum window GT-Score **> 0.3**
4. Hold-out evaluation (2024-01-01 to today)
   - Threshold: hold-out GT-Score **> 70% of walk-forward score** (allow max 30% decay)
5. If all pass: status = **'passed'**, record best parameters

**Output**: Database updated with validation results; console prints "PASS" or "FAIL: <reason>"

### Phase 3: Live Paper Trading
Deploy passed strategies to Oanda practice account:

```bash
python live_test.py mean_rev_eur_v1
```

**Trader workflow**:
1. Fetch strategy from database (must be 'passed')
2. Poll Oanda API every 60 seconds for new daily candles
3. Generate signals using best parameters (no re-optimization)
4. Place/close market orders automatically
5. Track equity curve and compute rolling GT-Score
6. Update database daily

Run indefinitely or until manually stopped (Ctrl+C).

---

## Architecture

### Modules

#### `pipeline_utils.py` – Core Engine
All reusable logic for strategy evaluation, database operations, and metrics.

**Key Functions**:
- `compute_gt_score(returns)` – GT-Score calculation
- `grid_search(data, strategy_func, param_grid)` – Parameter optimization
- `walk_forward(data, strategy_func, param_grid, n_windows)` – Multi-window validation
- `compute_strategy_fingerprint(code, param_grid)` – SHA256 deduplication
- `init_db()`, `check_idea_is_new()`, `insert_strategy()`, `record_validation()`, etc. – DB operations

#### `data_fetcher.py` – Oanda Integration
Fetches historical OHLC data from Oanda v20 REST API.

**Key Function**:
- `get_candles(instrument, granularity, start, end)` – Fetch with auto-pagination
- `get_candles_date_range(instrument, start_date, end_date)` – Convenience wrapper (YYYY-MM-DD format)

#### `validator.py` – Backtest Script
Main entry point for testing strategy candidates. Runs full validation pipeline.

**Usage**:
```bash
python validator.py <json_file>
```

#### `live_test.py` – Live Trader
Paper trades passed strategies on Oanda practice account. Polls for new candles and places orders.

**Usage**:
```bash
python live_test.py <strategy_id> [--instrument EUR_USD]
```

#### `.opencode/agents/researcher.md` – Research Agent Config
OpenCode subagent definition for generating strategy candidates with economic rationale.

### Database Schema (`pipeline.db`)

#### strategies
| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | e.g., "mean_rev_eur_v1" |
| fingerprint | TEXT UNIQUE | SHA256(code + param_grid) |
| code | TEXT | Python source (generate_signals function) |
| param_grid | TEXT | JSON of param dictionary |
| rationale | TEXT | One-sentence economic hypothesis |
| status | TEXT | 'proposed', 'research_failed', 'walk_forward_failed', 'holdout_failed', 'passed', 'paper_trading', 'live', 'retired' |
| created_at | TEXT | ISO timestamp |

#### validation_results
| Column | Type | Notes |
|--------|------|-------|
| strategy_id | TEXT PK FK | References strategies(id) |
| best_params | TEXT | JSON of best parameters from grid search |
| is_gt_score | REAL | In-sample GT-Score |
| walk_forward_gt_score | REAL | Combined OOS GT-Score |
| holdout_gt_score | REAL | Hold-out period GT-Score |
| final_status | TEXT | 'pass' or 'fail: <reason>' |
| tested_at | TEXT | ISO timestamp |

#### live_status
| Column | Type | Notes |
|--------|------|-------|
| strategy_id | TEXT PK FK | References strategies(id) |
| start_date | TEXT | When paper trading began |
| equity_curve | TEXT | JSON list of {date, equity} objects (append-only) |
| current_gt_score | REAL | Rolling GT-Score (last N days) |
| last_updated | TEXT | ISO timestamp |

---

## GT-Score Formula

GT-Score combines three metrics:

1. **Sharpe Ratio**: Annual return ÷ annual volatility
2. **Sortino Ratio**: Annual return ÷ downside deviation (negative returns only)
3. **Win Rate**: Fraction of positive periods

**Formula**:
```
GT-Score = (Sharpe + 2*Sortino + 2*(WinRate - 0.5)) / 3
```

Higher scores indicate better risk-adjusted returns with consistency.

---

## Key Design Rules (Enforced)

✅ **Researcher Blind Spot**: Researcher agent only knows pre-2020 financial principles (data cutoff: 2019-12-31). No future-knowledge leakage.

✅ **One-Shot Validation**: Once a candidate is validated, result is final. Never re-optimize after a fail (prevents overfitting).

✅ **Fingerprint Deduplication**: No strategy with the same code+param_grid can be submitted twice.

✅ **No Look-Ahead**: Validation uses strict walk-forward splits; each window trains only on prior data.

✅ **Paper Trading First**: Live forward testing is mandatory before real money deployment.

✅ **Single Metric**: All evaluation uses GT-Score. Consistent yardstick across in-sample, OOS, and hold-out.

---

## Validation Gates

| Gate | Threshold | Purpose |
|------|-----------|---------|
| In-Sample GT-Score | > 0.5 | Basic profitability check on dev data |
| Walk-Forward Combined GT-Score | > 1.0 | Stability across multiple time periods |
| Min Window GT-Score | > 0.3 | No single period catastrophically fails |
| Hold-Out Decay | < 30% relative decline | Recent OOS performance acceptable |

---

## Example: Mean Reversion on EUR/USD

### Step 1: Generate Candidate (Researcher Agent)
```bash
Researcher: Generate a mean reversion strategy for EUR_USD based on Bollinger Bands
```

**Response (JSON)**:
```json
{
  "strategy_id": "rsi_mean_rev_eur_v1",
  "code": "import pandas as pd\nfrom ta import momentum\n\ndef generate_signals(df, params):\n    lookback = params['lookback']\n    rsi_low = params['rsi_low']\n    rsi_high = params['rsi_high']\n    \n    rsi = momentum.rsi(df['close'], window=lookback)\n    \n    signal = pd.Series(0, index=df.index)\n    signal[rsi < rsi_low] = 1\n    signal[rsi > rsi_high] = -1\n    \n    return signal",
  "param_grid": {
    "lookback": [10, 14, 20],
    "rsi_low": [20, 30],
    "rsi_high": [70, 80]
  },
  "rationale": "RSI extremes mean-revert due to behavioral overreaction."
}
```

### Step 2: Save and Validate
```bash
# Save to file
cat > strategy_candidate.json << 'EOF'
{...}
EOF

# Run validator
python validator.py strategy_candidate.json
```

**Expected Output**:
```
======================================================================
Validating: rsi_mean_rev_eur_v1
Instrument: EUR_USD
Rationale: RSI extremes mean-revert due to behavioral overreaction.
======================================================================

[1/8] Checking for duplicate...
  Fingerprint: a3f7c2b1d9e4... (NEW)

[2/8] Inserting as proposed...
  OK

...

[8/8] Evaluating on hold-out (OOS) with best params...
  Hold-out GT-Score: 1.12

======================================================================
PASS: Strategy passed all validation gates
======================================================================
  In-sample GT-Score:      0.72
  Walk-forward GT-Score:   1.15
  Hold-out GT-Score:       1.12
  Best Parameters:         {'lookback': 14, 'rsi_low': 25, 'rsi_high': 75}
======================================================================
```

### Step 3: Live Trade
```bash
python live_test.py rsi_mean_rev_eur_v1 --instrument EUR_USD
```

**Output**:
```
======================================================================
Live Trader: rsi_mean_rev_eur_v1
Instrument: EUR_USD
Best Params: {'lookback': 14, 'rsi_low': 25, 'rsi_high': 75}
Rationale: RSI extremes mean-revert due to behavioral overreaction.
======================================================================

Starting live trading loop (polling every 60s)...

[2026-05-01T14:32:15.123456] Entered 1 position
[2026-05-01T15:00:00.000000] Daily return: +0.0045, Position: 1, P&L: +0.0045
[2026-05-02T15:00:00.000000] Metrics updated: equity=100234.50, GT-Score=1.08
...
```

---

## Troubleshooting

### Error: "OANDA_ACCOUNT_ID and OANDA_API_TOKEN env vars required"
Set environment variables before running:
```bash
export OANDA_ACCOUNT_ID="your_id"
export OANDA_API_TOKEN="your_token"
```

### Error: "Strategy {id} not found" in live_test.py
Ensure the strategy was validated successfully (status = 'passed'):
```python
from pipeline_utils import get_strategy_by_id
strat = get_strategy_by_id('mean_rev_eur_v1')
print(strat)  # Check status
```

### Error: "Code must define generate_signals(df, params)"
Ensure your strategy code exports a function named `generate_signals` with correct signature.

### Hold-Out Fails Unexpectedly
- Recent market regime may differ from historical. Acceptable if decay < 30%.
- If decay > 30%, strategy is too fragile; retire and restart with new hypothesis.

---

## Performance Tips

1. **Limit Parameter Grid**: Keep total combos ≤ 200 (e.g., 4 params × 5 values each)
2. **Use Vectorized Ops**: Avoid loops in signal generation; use pandas/numpy operations
3. **Polling Interval**: For daily strategies, 60-second polling is sufficient
4. **Position Size**: Start conservative (1000 units micro lot) on practice account

---

## License & Disclaimer

For research and education purposes. This system assumes disciplined execution and does NOT provide financial advice. Past performance is not indicative of future results. Always test thoroughly on paper trading before considering live money.

---

## Contact & Support

For questions or issues, refer to the code documentation in each module header.
