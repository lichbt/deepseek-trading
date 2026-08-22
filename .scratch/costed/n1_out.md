VERDICT: FAIL — no verdict; last output:
1. **Update `report()` in `oanda_book_simulator.py`** — needs separate spread/commission lines under `charge_spread`, a per-instrument commission breakdown (mirroring the swap breakdown), and a `UNCOSTED (charged 0): ...` line listing held instruments absent from `CTRADER_COMMISSION` when venue is ctrader.
2. **Update `scripts/risk_model_sim.py`** — 5 sites to modify (lines ~179, ~206, ~258 add one side commission; ~233 entry make sides=1 with price/quote_to_usd/venue; ~282 roll-flat make sides=2), plus add `comm_paid` to summary dict and the printed key tuple at the bottom of `main()`.
3. **Add new test class to `tests/test_costs.py`** — assert `_commission` against 6 real broker fills (USD_CHF 0.10, EUR_GBP 0.04, AUD_USD 0.28, XAG_USD 0.03, BTC_USD 0.19, NAS100_USD 0.00 each to within 1 cent at venue='ctrader', sides=1), plus OANDA XAU_USD still `units*0.30`, sides=2 doubles, absent instrument returns 0.0.
4. **Run acceptance steps 1-5** — `pytest tests/test_costs.py -q`, import checks, the `--check-baseline` reproduction test (must PASS with max diff ≤1e-6), and the charge-spread sanity check (comm_paid non-zero and negative).

## Recommendations for next session

Resume from `scripts/risk_model_sim.py` — the 4 charge sites there mirror the pattern already applied to `oanda_book_simulator.py` (split sp/cm, gate on in_evaluation, pass venue/price/quote_to_usd). Then the `report()` changes in `oanda_book_simulator.py`, then the tests, then run all 5 acceptance steps. The baseline reproduction (step 4) should be unaffected since all changes are gated on `charge_spread` or `charge_swap`+`roll_flat`, both off in `check_baseline`.
