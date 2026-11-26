"""Prop-challenge DAILY-DD breach Monte Carlo.

Reconstructs the CURRENT live book's daily PnL (portfolio.load_strategies →
conviction × Kelly × inverse-vol × decay), block-bootstraps N_SIM paths across
several horizons, and reports the probability that a *scaled* book breaches the
3% daily-DD limit. `scale` is a global multiplier on current position sizing
(1.0 = live sizing). Output columns:
  scale horizon block breach_pct p1_worst_day_pct p01_worst_day_pct median_worst_day_pct

Run from anywhere:  python scripts/prop_daily_breach_mc.py
Companion: prop_pass_curve_mc.py (pass-odds / +10% target). See memory
'prop-challenge-dd-sizing' and the Second Brain for the interpreted result.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import portfolio as P

START='2020-01-01'
END=pd.Timestamp.utcnow().strftime('%Y-%m-%d')
SCALES=[1.0,1.5,2.0,2.5,3.0,3.5,4.0,4.5,5.0]
N_SIM=50000
BLOCKS=[1,5,10,20]
HORIZONS=[60,252,504,756]
SEED=777
DAILY=-0.03

strategies=P.load_strategies(0.0)
rets={}; sigs={}; wf={r['id']:r['walk_forward_gt_score'] for r in strategies}
for r in strategies:
    built=P.build_strategy_returns(r, START, END)
    if built:
        rets[r['id']], sigs[r['id']] = built
weights, vols, decay = P.inverse_vol_weights(rets, sigs, wf)
corr=P.compute_correlation_matrix(rets)
flags=P.flag_correlated_pairs(corr,wf)
for sid in {w for *_, w in flags}:
    if sid in weights: weights[sid]*=0.5
s=sum(weights.values())
if s: weights={k:v/s for k,v in weights.items()}
weights=P._apply_cluster_caps(weights)

daily=None
for sid,r in rets.items():
    x = r * weights.get(sid,0) * P.scheduled_decay_scale(r,sigs[sid],wf.get(sid,0.0)) * P.kelly_scale(r)
    daily = x if daily is None else daily.add(x, fill_value=0)
daily = daily.fillna(0).loc[pd.Timestamp(START):pd.Timestamp(END)]
arr=daily.values.astype(float)
arr=arr[np.isfinite(arr)]

print('base_worst_pct', round(arr.min()*100,3), 'days', len(arr))
print('scale horizon block breach_pct p1_worst_day_pct p01_worst_day_pct median_worst_day_pct')

def sample_block(rng,n,block):
    if block<=1:
        return rng.choice(arr,size=n,replace=True)
    chunks=[]; total=0
    while total<n:
        st=rng.integers(0,max(1,len(arr)-block+1))
        c=arr[st:st+block]
        chunks.append(c); total+=len(c)
    return np.concatenate(chunks)[:n]

for horizon in HORIZONS:
  for block in BLOCKS:
    rng=np.random.default_rng(SEED+horizon*100+block)
    base=np.empty((N_SIM,horizon),dtype=float)
    for i in range(N_SIM):
        base[i]=sample_block(rng,horizon,block)
    for scale in SCALES:
        worst=(base*scale).min(axis=1)
        print(scale,horizon,block,
              round((worst<=DAILY).mean()*100,3),
              round(np.quantile(worst,0.01)*100,3),
              round(np.quantile(worst,0.001)*100,3),
              round(np.median(worst)*100,3))
