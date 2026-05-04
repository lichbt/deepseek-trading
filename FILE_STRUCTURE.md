#!/bin/bash

# File structure index for Trading Strategy Research & Validation Pipeline
# Generated: 2026-05-01

cat << 'EOF'
├── Core Modules
│   ├── pipeline_utils.py              # Foundation: GT-Score, grid search, walk-forward, DB ops
│   ├── data_fetcher.py                # Oanda API integration (OHLC fetching)
│   ├── validator.py                   # Backtest script (4-gate validation filter)
│   └── live_test.py                   # Paper trading script (Oanda automated orders)
│
├── Agent Configuration
│   └── .opencode/agents/
│       └── researcher.md              # OpenCode subagent: strategy generation (pre-2020 constraint)
│
├── Documentation (Read in Order)
│   ├── README.md                      # Overview, architecture, schema, quick start
│   ├── QUICKSTART.md                  # Step-by-step setup and usage guide
│   ├── ARCHITECTURE.md                # Detailed system design, data flow, algorithms
│   ├── CONFIGURATION.md               # Tuning guide, customization, troubleshooting
│   └── PROJECT_COMPLETION_SUMMARY.md  # Deliverables, file descriptions, checklist
│
├── Support Files
│   ├── requirements.txt               # Python dependencies
│   ├── .env.example                   # Environment variable template
│   ├── sample_strategy.json           # Example RSI mean reversion strategy
│   ├── setup_verify.py                # Setup verification script
│   ├── Makefile                       # Convenience commands
│   └── FILE_STRUCTURE.md              # This file
│
└── Generated at Runtime
    └── pipeline.db                    # SQLite database (created by setup_verify.py or init_db())
        ├── strategies table           # Strategy candidates + metadata
        ├── validation_results table   # Backtest results (GT-Scores, best params)
        └── live_status table          # Paper trading equity curve + metrics

═══════════════════════════════════════════════════════════════════════════════

USAGE WORKFLOW:

1. SETUP (One-Time)
   $ make setup                    # Install deps, verify, init DB
   $ make verify                   # Check all systems ready

2. GENERATE STRATEGY (Interactive)
   @researcher Generate mean reversion strategy for EUR_USD
   → Output: strategy_candidate.json

3. VALIDATE STRATEGY (Backtesting)
   $ python validator.py strategy_candidate.json
   → Output: "PASS" or "FAIL: <reason>" + DB updates

4. DEPLOY STRATEGY (Live Trading)
   $ python live_test.py strategy_id
   → Output: Continuous paper trading on Oanda practice

═══════════════════════════════════════════════════════════════════════════════

QUICK REFERENCE:

Core Functions (pipeline_utils.py):
  compute_gt_score(returns)           → float (0.3–3.0 typical)
  grid_search(data, func, grid)       → (best_params, score)
  walk_forward(data, func, grid, n)   → {combined_score, per_window, oos_returns}
  evaluate_on_data(data, func, params)→ float
  compute_strategy_fingerprint(code, grid) → str (SHA256)

Database Functions (pipeline_utils.py):
  init_db()                           → Create tables
  check_idea_is_new(fingerprint)      → {new: bool, status?: str}
  insert_strategy(...)                → Propose strategy
  record_validation(...)              → Save backtest results
  start_live_trading(strategy_id)     → Begin paper trading
  update_live_metrics(...)            → Update equity + GT-Score
  get_passed_strategies()             → List[Dict]
  get_strategy_by_id(strategy_id)     → Dict

Data Fetching (data_fetcher.py):
  get_candles(instrument, granularity, start, end) → DataFrame
  get_candles_date_range(instrument, start_date, end_date) → DataFrame

Validation Thresholds (validator.py):
  In-Sample (grid search)             > 0.5
  Walk-Forward (combined)             > 1.0
  Walk-Forward (min window)           > 0.3
  Hold-Out (decay)                    < 30%

Time Periods (validator.py):
  In-Sample (Dev)                     2015-01-01 to 2019-12-31
  Walk-Forward                        2015-01-01 to 2023-12-31
  Hold-Out (OOS)                      2024-01-01 to today

═══════════════════════════════════════════════════════════════════════════════

CONFIGURATION KNOBS:

In validator.py:
  DEV_START, DEV_END                  → In-sample period
  HOLDOUT_START                       → Hold-out start date
  MIN_IS_SCORE, MIN_WF_SCORE, etc.    → GT-Score thresholds

In pipeline_utils.py walk_forward():
  n_windows, train_length, test_length → Window configuration

In live_test.py:
  POSITION_SIZE                       → Units per trade (1000 = micro lot)
  POLLING_INTERVAL                    → Seconds between polls (60)
  ROLLING_GT_WINDOW                   → Days for rolling metric (30)

═══════════════════════════════════════════════════════════════════════════════

ENVIRONMENT VARIABLES:

Required:
  OANDA_ACCOUNT_ID                    # e.g., 101-001-12345678-001
  OANDA_API_TOKEN                     # v20 API token from developer.oanda.com

Optional:
  Set in .env or shell before running scripts

═══════════════════════════════════════════════════════════════════════════════

DATABASE SCHEMA:

strategies:
  id (PK) | fingerprint (UNIQUE) | code | param_grid | rationale | status | created_at

validation_results:
  strategy_id (PK FK) | best_params | is_gt_score | walk_forward_gt_score | 
  holdout_gt_score | final_status | tested_at

live_status:
  strategy_id (PK FK) | start_date | equity_curve | current_gt_score | last_updated

Status Values:
  proposed | research_failed | walk_forward_failed | holdout_failed | 
  passed | paper_trading | live | retired

═══════════════════════════════════════════════════════════════════════════════

TROUBLESHOOTING:

"OANDA credentials not set"
  → export OANDA_ACCOUNT_ID="..."; export OANDA_API_TOKEN="..."

"generate_signals not found"
  → Strategy code must define: def generate_signals(df, params): return signal

"Hold-out test failed with high decay"
  → Change strategy hypothesis or parameters, resubmit (new fingerprint)

"No candles returned from Oanda"
  → Check instrument name (EUR_USD, not eurusd), date range valid

More help:
  → See CONFIGURATION.md troubleshooting section

═══════════════════════════════════════════════════════════════════════════════

FILES BY PURPOSE:

Data & Core:
  pipeline_utils.py                   Core engine (GT-Score, optimization, DB)
  data_fetcher.py                     Oanda API client

Workflows:
  validator.py                        Backtest & validate strategies
  live_test.py                        Paper trade on Oanda

Configuration:
  researcher.md                       Researcher agent (strategy generation)
  requirements.txt                    Python dependencies
  .env.example                        Credentials template

Documentation:
  README.md                           Getting started
  QUICKSTART.md                       Step-by-step guide
  ARCHITECTURE.md                     Deep dive (design, algorithms)
  CONFIGURATION.md                    Tuning & customization
  PROJECT_COMPLETION_SUMMARY.md       Deliverables summary

Support:
  setup_verify.py                     Pre-flight check
  Makefile                            Convenience commands
  sample_strategy.json                Example strategy template
  FILE_STRUCTURE.md                   This file

═══════════════════════════════════════════════════════════════════════════════

NEXT STEPS:

1. Read README.md for overview
2. Run: make setup
3. Run: make verify
4. Ask Researcher for strategy
5. Run: python validator.py <strategy.json>
6. If PASS: python live_test.py <strategy_id>
7. Monitor database daily

═══════════════════════════════════════════════════════════════════════════════

For detailed information, start with:
  README.md           (project overview)
  QUICKSTART.md       (step-by-step usage)
  ARCHITECTURE.md     (system design)
  CONFIGURATION.md    (tuning & troubleshooting)

EOF
