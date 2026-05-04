# Quick Start Guide

## 1. Setup (One-Time)

### Install Dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Set Environment Variables
```bash
export OANDA_ACCOUNT_ID="your_practice_account_id"
export OANDA_API_TOKEN="your_v20_api_token"
```

Or create a `.env` file and source it:
```bash
cp .env.example .env
# Edit .env with your credentials
source .env
```

### Verify Setup
```bash
python setup_verify.py
```

Expected output: All checks PASS.

---

## 2. Generate Strategy Candidate

Use the **Researcher Agent** to generate a strategy:

**In VS Code Chat:**
```
@researcher Generate a mean reversion strategy for EUR_USD using RSI
```

Or specify market domain:
```
@researcher Generate a momentum strategy for GBP_USD with moving average crossover
```

**Agent Output:** JSON file with strategy candidate

Example structure:
```json
{
  "strategy_id": "mean_rev_eur_v1",
  "code": "def generate_signals(df, params):\n    ...",
  "param_grid": {"lookback": [10, 20, 30]},
  "rationale": "Economic hypothesis"
}
```

---

## 3. Validate Strategy (Backtest)

```bash
python validator.py strategy_candidate.json
```

**Workflow**:
1. Check fingerprint (avoid duplicates)
2. Grid search on 2015-2019 data (in-sample)
   - Threshold: GT-Score > 0.5
3. Walk-forward on 2015-2023 data (no look-ahead)
   - Threshold: GT-Score > 1.0
4. Hold-out test on 2024+ data (OOS)
   - Threshold: < 30% decay from walk-forward

**Output**:
- Database updated with results
- Console: "PASS" or "FAIL: <reason>"
- Status stored: 'passed', 'research_failed', 'walk_forward_failed', or 'holdout_failed'

**Example PASS Output**:
```
======================================================================
PASS: Strategy passed all validation gates
======================================================================
  In-sample GT-Score:      0.72
  Walk-forward GT-Score:   1.15
  Hold-out GT-Score:       1.12
  Best Parameters:         {'lookback': 14, 'rsi_low': 25, 'rsi_high': 75}
======================================================================
```

---

## 4. Live Paper Trade (Optional)

Once a strategy **PASSES** validation, deploy it:

```bash
python live_test.py mean_rev_eur_v1
```

**Trader Workflow**:
1. Fetch strategy from database (must be 'passed')
2. Poll Oanda API every 60 seconds for new candles
3. Generate signals using best parameters
4. Place/close market orders automatically
5. Track equity curve and rolling GT-Score
6. Update database daily

**Stop with**: `Ctrl+C`

**Example Output**:
```
======================================================================
Live Trader: mean_rev_eur_v1
Instrument: EUR_USD
Best Params: {'lookback': 14, 'rsi_low': 25, 'rsi_high': 75}
======================================================================

Starting live trading loop (polling every 60s)...

[2026-05-01T14:32:15.123456] Entered 1 position
[2026-05-01T15:00:00.000000] Daily return: +0.0045, Position: 1, P&L: +0.0045
[2026-05-02T15:00:00.000000] Metrics updated: equity=100234.50, GT-Score=1.08
```

---

## 5. Monitor Results

Query database for strategy status:

```python
from pipeline_utils import get_strategy_by_id, get_passed_strategies

# Check specific strategy
strat = get_strategy_by_id('mean_rev_eur_v1')
print(f"Status: {strat['status']}")

# List all passed strategies
passed = get_passed_strategies()
for s in passed:
    print(f"  {s['id']}: {s['best_params']}")
```

---

## Common Workflows

### Workflow A: Single Strategy Test
```bash
# 1. Generate with Researcher
Researcher: Generate mean reversion EUR_USD

# 2. Validate
python validator.py strategy.json

# 3. If PASS → Live trade
python live_test.py strategy_id
```

### Workflow B: Multiple Strategy Candidates
```bash
# Generate batch of candidates
Researcher: Generate 3 different momentum strategies for XAU_USD

# Validate each
python validator.py strat1.json
python validator.py strat2.json
python validator.py strat3.json

# Track which passed in database
python -c "from pipeline_utils import get_passed_strategies; \
           for s in get_passed_strategies(): print(s['id'])"
```

### Workflow C: Strategy Retirement
If live trading underperforms (hold-out decay > 30% expected), mark as retired:

```python
from pipeline_utils import get_db_connection

with get_db_connection() as conn:
    conn.execute("UPDATE strategies SET status = 'retired' WHERE id = 'mean_rev_eur_v1'")
```

---

## Troubleshooting

### "OANDA credentials not set"
```bash
export OANDA_ACCOUNT_ID="your_id"
export OANDA_API_TOKEN="your_token"
```

### "generate_signals not found"
Ensure strategy code defines:
```python
def generate_signals(df, params):
    # Must return pd.Series of 1, -1, or 0
    return signal
```

### "Hold-out test failed with high decay"
Strategy may be overfitted or regime has changed. Options:
1. Accept if decay ≤ 30% (expected OOS performance drop)
2. Modify strategy hypothesis and resubmit with new fingerprint
3. Retire strategy and start fresh

### "No candles returned from Oanda"
Check:
- Oanda API token is valid
- Instrument name is correct (e.g., EUR_USD, not eurusd)
- Date range is available (Oanda has limited historical data)

---

## Configuration

### Data Periods (hardcoded in validator.py)
- **Dev (In-Sample)**: 2015-01-01 to 2019-12-31
- **Walk-Forward**: 2015-01-01 to 2023-12-31 (excluding hold-out)
- **Hold-Out**: 2024-01-01 to latest

### GT-Score Thresholds
- In-sample: > 0.5
- Walk-forward combined: > 1.0
- Min window: > 0.3
- Hold-out decay: < 30% relative

### Polling & Position Size
- Poll interval: 60 seconds
- Position size: 1000 units (EUR micro lot)
- Rolling GT-Score window: 30 days
- Metrics update: daily

---

## Example: Mean Reversion with RSI

### JSON Candidate:
```json
{
  "strategy_id": "rsi_mean_rev_eur_v1",
  "code": "import pandas as pd\nfrom ta import momentum\n\ndef generate_signals(df, params):\n    rsi = momentum.rsi(df['close'], window=params['lookback'])\n    signal = pd.Series(0, index=df.index)\n    signal[rsi < params['rsi_low']] = 1\n    signal[rsi > params['rsi_high']] = -1\n    return signal",
  "param_grid": {
    "lookback": [10, 14, 20],
    "rsi_low": [20, 30],
    "rsi_high": [70, 80]
  },
  "rationale": "RSI extremes mean-revert due to behavioral overreaction."
}
```

### Validate:
```bash
python validator.py rsi_strategy.json
```

### Deploy:
```bash
python live_test.py rsi_mean_rev_eur_v1 --instrument EUR_USD
```

---

## References

- **GT-Score**: Alexander Sheppert's risk-adjusted metric (Sharpe + Sortino + Win Rate)
- **Walk-Forward**: Multi-window backtesting to avoid look-ahead bias
- **Oanda API**: https://developer.oanda.com/rest-live-v20/introduction/
- **TA-Lib**: Technical analysis library for indicators (RSI, MACD, etc.)

---

## Next Steps

1. ✓ Run `setup_verify.py` to confirm installation
2. ✓ Generate first strategy with Researcher agent
3. ✓ Test with `validator.py`
4. ✓ If PASS, deploy with `live_test.py`
5. ✓ Monitor equity curve in database

Happy trading! 📈
