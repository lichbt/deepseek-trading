# Configuration & Customization

## Core Configuration

### Validator Time Periods (in `validator.py`)

```python
# Lines ~45-47
DEV_START = '2015-01-01'      # In-sample training data start
DEV_END = '2019-12-31'         # In-sample training data end (researcher cutoff)
HOLDOUT_START = '2024-01-01'   # Out-of-sample hold-out starts here
```

**Rationale**:
- **Dev (2015-2019)**: Pre-2020 period ensures researcher has no future knowledge
- **Walk-Forward**: 2015 to Dec 2023 (excludes hold-out)
- **Hold-Out (2024+)**: Truly unseen recent data for final robustness check

### GT-Score Thresholds (in `validator.py`)

```python
# Lines ~50-53
MIN_IS_SCORE = 0.5                # In-sample minimum
MIN_WF_SCORE = 1.0                # Walk-forward combined minimum
MIN_WINDOW_SCORE = 0.3            # Min per-window OOS score
HOLDOUT_DECLINE_THRESHOLD = 0.7   # 30% max decay allowed (multiplier)
```

**Tuning Guide**:
- **Higher MIN_IS_SCORE** (e.g., 0.7): Stricter in-sample filter, fewer strategies pass
- **Higher MIN_WF_SCORE** (e.g., 1.5): Tougher OOS bar, only best strategies pass
- **Higher MIN_WINDOW_SCORE** (e.g., 0.5): Prevents regime-specific strategies
- **Higher HOLDOUT_DECLINE_THRESHOLD** (e.g., 0.8): Allow up to 20% decay

### Walk-Forward Windows (in `pipeline_utils.py`)

```python
# Line ~320 in walk_forward()
n_windows=5,           # Number of train+test windows
train_length=1000,     # ~4 years of training data
test_length=250,       # ~1 year of testing data per window
```

**Impact**:
- More windows = more robust but slower
- Longer train = more parameters, risk of overfitting
- Longer test = more OOS data, better validation

### Live Trading Parameters (in `live_test.py`)

```python
# Lines ~35-40
ROLLING_WINDOW_SIZE = 500        # Keep last 500 candles in memory
POLLING_INTERVAL = 60            # Poll Oanda every 60 seconds
POSITION_SIZE = 1000             # 1000 EUR micro lot per trade
ROLLING_GT_WINDOW = 30           # Compute GT-Score over last 30 days
UPDATE_INTERVAL = 86400          # Update metrics every 86400 sec (1 day)
```

**Tuning Guide**:
- **POSITION_SIZE**: Increase for larger positions, but start small (1000 = 0.01 lots)
- **POLLING_INTERVAL**: 60s for daily strategies, 300s for 1-hour strategies
- **ROLLING_GT_WINDOW**: Longer = smoother metric, shorter = more reactive

---

## Researcher Agent Configuration

### Domain Override
By default, Researcher chooses interesting markets. Force a specific domain:

```
Researcher (domain="EUR_USD"): Generate mean reversion strategy
Researcher (domain="XAU_USD"): Generate momentum strategy
```

### Parameter Constraints
Researcher enforces (built into `.opencode/agents/researcher.md`):
- Max 4 parameters in grid
- Total combinations ≤ 200
- Max 5 logical conditions in strategy function
- No post-2019 data references

### Rationale Quality
Researcher self-critiques before output. Examples of good rationales:
- ✓ "Mean reversion exploits overbought/oversold extremes due to behavioral overreaction"
- ✓ "Momentum persists through portfolio rebalancing flows"
- ✗ "Go long when price is high" (vague, no economics)
- ✗ "Strategy that uses FOMC meetings to predict moves" (post-2019 knowledge)

---

## Strategy Fingerprinting

### How It Works
Fingerprints are SHA256 hashes of:
```
code (as string) + json.dumps(param_grid, sort_keys=True)
```

### Implications
- **Same code, same params** → Same fingerprint (rejected if duplicate exists)
- **Same code, different params** → Different fingerprint (allowed)
- **Different code, same idea** → Different fingerprint (allowed, but defeats purpose)

### Re-submission Rules
If a strategy fails:
1. **Cannot re-validate** with same code+params (fingerprint unchanged)
2. **Can modify** code or params (new fingerprint)
3. **Recommended**: Change the core hypothesis or parameter space

---

## Database Customization

### Connection String
Edit `DB_PATH` in `pipeline_utils.py` (default: `pipeline.db` in script dir):

```python
# Line ~168
DB_PATH = Path(__file__).parent / 'pipeline.db'

# Or:
DB_PATH = Path('/custom/path/my_strategies.db')
```

### Adding Fields
To add a column to `strategies` table:

```python
# In pipeline_utils.py, modify init_db():
cursor.execute('''
    CREATE TABLE IF NOT EXISTS strategies (
        id TEXT PRIMARY KEY,
        fingerprint TEXT UNIQUE NOT NULL,
        code TEXT NOT NULL,
        param_grid TEXT NOT NULL,
        rationale TEXT,
        status TEXT NOT NULL DEFAULT 'proposed',
        created_at TEXT NOT NULL,
        custom_field TEXT,  # <-- Add here
        ...
    )
''')
```

Then update all related functions (`insert_strategy`, `get_strategy_by_id`, etc.).

### Backup Strategy
```bash
# Copy database before risky operations
cp pipeline.db pipeline.db.backup

# Query with sqlite3 CLI
sqlite3 pipeline.db "SELECT id, status FROM strategies;"
```

---

## Oanda API Setup

### Create Practice Account
1. Visit https://developer.oanda.com
2. Sign up for free practice account
3. Generate v20 API token
4. Note Account ID (format: `101-001-XXXXXXX-001`)

### Environment Variables
```bash
# Add to ~/.bashrc, ~/.zshrc, or .env file
export OANDA_ACCOUNT_ID="101-001-12345678-001"
export OANDA_API_TOKEN="abc123def456..."
```

### Verify Credentials
```python
import os
from data_fetcher import get_candles_date_range

# Test fetch
df = get_candles_date_range('EUR_USD', '2020-01-01', '2020-01-31')
print(f"Fetched {len(df)} candles")  # Should print > 0
```

---

## Instrument Selection

### Supported Instruments on Oanda
| Code | Name | Historical Data |
|------|------|------------------|
| EUR_USD | Euro / US Dollar | Yes (2005+) |
| GBP_USD | British Pound / US Dollar | Yes (2005+) |
| USD_JPY | US Dollar / Japanese Yen | Yes (2005+) |
| AUD_USD | Australian Dollar / US Dollar | Yes (2005+) |
| XAU_USD | Gold / US Dollar | Yes (2015+) |
| SPX500 | S&P 500 Index | Yes (2015+) |

### Strategy Example by Instrument
```
# EUR/USD: 4+ years of dense data, very liquid
EUR_USD: Good for mean reversion, high volume

# XAU_USD: Commodity, lower volume
XAU_USD: Good for momentum, fewer edge cases

# SPX500: Index, distinct regimes (trending vs consolidating)
SPX500: Good for regime-detection strategies
```

---

## Custom Strategy Templates

### Template 1: Simple Moving Average Crossover
```python
def generate_signals(df, params):
    fast = params['fast_period']
    slow = params['slow_period']
    
    ma_fast = df['close'].rolling(fast).mean()
    ma_slow = df['close'].rolling(slow).mean()
    
    signal = pd.Series(0, index=df.index)
    signal[ma_fast > ma_slow] = 1   # Long
    signal[ma_fast < ma_slow] = -1  # Short
    
    return signal
```

### Template 2: RSI-Based Mean Reversion
```python
def generate_signals(df, params):
    from ta import momentum
    
    lookback = params['lookback']
    rsi_low = params['rsi_low']
    rsi_high = params['rsi_high']
    
    rsi = momentum.rsi(df['close'], window=lookback)
    
    signal = pd.Series(0, index=df.index)
    signal[rsi < rsi_low] = 1   # Oversold → long
    signal[rsi > rsi_high] = -1 # Overbought → short
    
    return signal
```

### Template 3: Bollinger Band Squeeze
```python
def generate_signals(df, params):
    lookback = params['lookback']
    num_std = params['num_std']
    
    sma = df['close'].rolling(lookback).mean()
    std = df['close'].rolling(lookback).std()
    
    upper = sma + num_std * std
    lower = sma - num_std * std
    
    signal = pd.Series(0, index=df.index)
    signal[df['close'] > upper] = 1   # Breakout → long
    signal[df['close'] < lower] = -1  # Breakdown → short
    
    return signal
```

---

## Performance Tuning

### Speed Up Grid Search
1. **Reduce param grid** (fewer combinations)
   - From 4×5×5×5 = 500 → 3×4×4 = 48 combos
2. **Use simpler strategy** (fewer calculations per bar)
   - Avoid complex indicators, use basic math
3. **Pre-cache Oanda data**
   - Don't re-fetch if data unchanged

### Speed Up Walk-Forward
1. **Fewer windows** (e.g., 3 instead of 5)
   - Trade-off: less robust but faster
2. **Shorter data windows** (e.g., 500 vs 1000 rows)
   - Trade-off: less historical context
3. **Parallel processing** (advanced)
   - Use multiprocessing for window evaluation

### Example: Fast Strategy
```python
# Use only close price, simple math
def generate_signals(df, params):
    lookback = params['lookback']
    threshold = params['threshold']
    
    close_ret = df['close'].pct_change(lookback)
    
    signal = pd.Series(0, index=df.index)
    signal[close_ret < -threshold] = 1  # Oversold
    signal[close_ret > threshold] = -1  # Overbought
    
    return signal
```

**Speed**: ~10x faster than multi-indicator strategies.

---

## Monitoring & Alerts

### Query Strategy Status
```python
from pipeline_utils import get_strategy_by_id, get_passed_strategies, get_db_connection

# Check one strategy
strat = get_strategy_by_id('mean_rev_eur_v1')
print(f"Status: {strat['status']}")
print(f"Params: {strat['best_params']}")

# List all passed
passed = get_passed_strategies()
for s in passed:
    print(f"  {s['id']}: {s['best_params']}")

# Custom SQL query
with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM strategies WHERE status = 'passed'")
    for row in cursor.fetchall():
        print(f"  {row['id']}: {row['status']}")
```

### Live Trading Metrics
```python
# Query live status
with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM live_status WHERE strategy_id = 'mean_rev_eur_v1'")
    row = cursor.fetchone()
    print(f"Current equity: {row['equity_curve']}")  # JSON
    print(f"GT-Score (rolling): {row['current_gt_score']}")
```

---

## Debugging

### Enable Logging
Add print statements in key functions:

```python
# In pipeline_utils.py, grid_search():
print(f"  Testing params: {params}")
print(f"  Score: {score:.4f}")
```

### Test Strategy Function
```python
import pandas as pd
from data_fetcher import get_candles_date_range

# Load data
df = get_candles_date_range('EUR_USD', '2015-01-01', '2015-12-31')

# Define strategy
def generate_signals(df, params):
    # Your code here
    return pd.Series(0, index=df.index)

# Test
signals = generate_signals(df, {'lookback': 20})
print(f"Signals: {signals.value_counts()}")  # Should have 1, -1, 0
```

### Validate GT-Score Calculation
```python
import pandas as pd
from pipeline_utils import compute_strategy_returns, compute_gt_score

# Create synthetic returns
returns = pd.Series([0.01, -0.02, 0.03, 0.01, -0.01])

# Compute GT-Score
gt = compute_gt_score(returns)
print(f"GT-Score: {gt:.4f}")  # Should be positive
```

---

## Deployment

### Production Checklist
- [ ] Oanda credentials set in environment
- [ ] Database backed up (`pipeline.db.backup`)
- [ ] First strategy validated successfully
- [ ] Live trader tested on practice account (no real money!)
- [ ] Monitoring script ready (query live_status regularly)
- [ ] Position size appropriate for account (start small)
- [ ] Error handling tested (network disconnects, API errors)

### Scaling to Multiple Strategies
```bash
# Validate multiple strategies
python validator.py strat1.json
python validator.py strat2.json
python validator.py strat3.json

# Deploy in separate terminals
# Terminal 1:
python live_test.py strategy_1

# Terminal 2:
python live_test.py strategy_2

# Monitor all in database
python -c "from pipeline_utils import get_passed_strategies; \
           print(len(get_passed_strategies()), 'strategies trading')"
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Fingerprint already exists" | Change strategy code or parameters (new fingerprint) |
| "In-sample score too low" | Modify strategy hypothesis, loosen params, try different market |
| "Hold-out fails with 40% decay" | Strategy is too fragile; retire and try new approach |
| "Oanda API error: invalid token" | Regenerate API token, verify env vars set |
| "No candles returned" | Check instrument name (e.g., EUR_USD not eurusd), date range valid |
| "Live trader won't start" | Ensure strategy status = 'passed' in DB |
| "Orders not placing" | Check position_size, account has sufficient margin |

