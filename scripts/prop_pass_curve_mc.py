"""Prop-challenge PASS-ODDS Monte Carlo.

Same current-book reconstruction as prop_daily_breach_mc.py, but classifies each
block-bootstrapped path as pass (+10% before any breach) / daily-breach (<=-3%
day) / total-breach (<=-10% DD) / timeout, per global sizing `scale`. Output:
  scale horizon block pass_pct daily_pct total_pct timeout_pct median_pass_day p25 p75 median_end_pct

Run from anywhere:  python scripts/prop_pass_curve_mc.py
Companion: prop_daily_breach_mc.py (daily-DD breach curve). See memory
'prop-challenge-dd-sizing' and the Second Brain for the interpreted result.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
import portfolio as P

START='2020-01-01'
END=pd.Timestamp.utcnow().strftime('%Y-%m-%d')
SCALES=[2.5,3.0,3.5,4.0]
N_SIM=30000
BLOCKS=[1,5,10,20]
HORIZONS=[60,120,180,252]
SEED=999
PROFIT=0.10
DAILY=-0.03
TOTAL=-0.10

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

def sample_block(rng,n,block):
    if block<=1:
        return rng.choice(arr,size=n,replace=True)
    chunks=[]; total=0
    while total<n:
        st=rng.integers(0,max(1,len(arr)-block+1))
        c=arr[st:st+block]
        chunks.append(c); total+=len(c)
    return np.concatenate(chunks)[:n]

def classify(path):
    eq=1.0; peak=1.0
    for i,r in enumerate(path,1):
        if r <= DAILY:
            return 'daily', i, eq-1
        eq *= 1+r
        peak=max(peak,eq)
        if eq/peak-1 <= TOTAL:
            return 'total', i, eq-1
        if eq-1 >= PROFIT:
            return 'pass', i, eq-1
    return 'timeout', len(path), eq-1

print('scale horizon block pass_pct daily_pct total_pct timeout_pct median_pass_day p25_pass_day p75_pass_day median_end_pct')
for horizon in HORIZONS:
  for block in BLOCKS:
    rng=np.random.default_rng(SEED+horizon*100+block)
    base=np.empty((N_SIM,horizon),dtype=float)
    for i in range(N_SIM):
        base[i]=sample_block(rng,horizon,block)
    for scale in SCALES:
        outcomes=[classify(p*scale) for p in base]
        kinds=[o[0] for o in outcomes]
        pass_days=np.array([o[1] for o in outcomes if o[0]=='pass'])
        ends=np.array([o[2] for o in outcomes])
        if len(pass_days):
            med=int(np.median(pass_days)); p25=int(np.quantile(pass_days,0.25)); p75=int(np.quantile(pass_days,0.75))
        else:
            med=p25=p75='na'
        print(scale,horizon,block,
              round(100*kinds.count('pass')/N_SIM,2),
              round(100*kinds.count('daily')/N_SIM,2),
              round(100*kinds.count('total')/N_SIM,2),
              round(100*kinds.count('timeout')/N_SIM,2),
              med,p25,p75,round(100*np.median(ends),2))
