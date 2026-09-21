"""Alibaba MaaS price table (USD per 1M tokens) and per-call cost.

Transcribed from the console price list on 2026-08-22. Prices change; re-check
before trusting a dollar figure, and note the two structural facts that decide
model choice here:

  * deepseek-v4-flash-0731 is EXACTLY 1/3 of deepseek-v4-pro-0813 on every line
    (input, output, cached input) at both peak and off-peak. Flash at PEAK is
    still cheaper than pro at OFF-PEAK, so the thesis head swap wins in every
    combination of hour and model.
  * Only the deepseek models have a peak/off-peak split (off-peak = half). The
    qwen and glm lines are flat, so the night window discounts the thesis and
    codegen legs but NOT the qwen critique leg.

Cached input is ~1/10 of uncached on deepseek and ~1/5 on qwen, which is why
prefix-cache layout is the dominant per-token lever — see the 2026-08-22
decision on family-specific caching.
"""
from datetime import datetime, timezone

# WHAT THIS ENDPOINT ACTUALLY SERVES (probed 2026-08-22 via GET /models on
# token-plan.ap-southeast-1.maas.aliyuncs.com):
#   deepseek-v4-flash-0731, deepseek-v4-pro, glm-5.2, qwen3.6-flash,
#   qwen3.7-plus, qwen3.7-max, qwen3.8-max  (+ audio/image models)
# The console prices the FULL Model Studio catalog, which is a superset: e.g.
# qwen3.7-flash is priced at $0.03/M (13x cheaper than the critique head) but
# returns HTTP 404 "Model not exist." here, on both the alias and the dated
# qwen3.7-flash-2026-07-15 id.
#
# TREAT THAT LIST AS A FLOOR, NOT A CEILING: deepseek-v4-pro-0813 serves this
# pipeline's code-gen and returns real cache hits, yet does NOT appear in it.
# Absence from /models proves nothing; only a direct call does.

# model -> (input, output, cached_input) USD per 1M tokens, at PEAK rate.
# Models with a peak/off-peak split carry offpeak=True and halve outside
# OFFPEAK_START..OFFPEAK_END.
PRICES = {
    # DeepSeek first-party (api.deepseek.com) — wired as the off-peak PRIMARY
    # 2026-09-12. Official rate card (api-docs.deepseek.com/quick_start/pricing):
    # off-peak = half of peak; peak = 01:00-04:00 & 06:00-10:00 UTC Mon-Fri.
    #   flash:  in $0.15 miss / $0.003 hit, out $0.60 (off-peak)
    #   v4-pro: in $0.66 miss / $0.022 hit, out $1.98 (off-peak)
    #   peak doubles those. Both carry offpeak=True.
    'deepseek-flash':         dict(inp=0.30, out=1.20, cached=0.006, offpeak=True),
    'deepseek-v4-pro':        dict(inp=1.32, out=3.96, cached=0.044, offpeak=True),
    'deepseek-v4-pro-0813':   dict(inp=1.32, out=3.96, cached=0.132, offpeak=True),
    'deepseek-v4-flash-0731': dict(inp=0.44, out=1.32, cached=0.044, offpeak=True),
    'qwen3.7-plus':           dict(inp=0.40, out=1.60, cached=0.08),
    # The qwen flash tiers are priced by PROMPT LENGTH. Both entries below use
    # the SHORT-context tier, which is safe here because _call_openrouter_once
    # hard-refuses any prompt over 12,000 tokens — this pipeline can never reach
    # the 32K boundary where qwen3.7-flash jumps to $0.10/$0.40.
    'qwen3.7-flash':          dict(inp=0.030, out=0.130, cached=0.030),
    'qwen3.7-flash-2026-07-15': dict(inp=0.030, out=0.130, cached=0.030),
    # qwen3.8-flash — CRITIQUE HEAD as of 2026-08-29. These numbers are an
    # UNVERIFIED CONSERVATIVE UPPER BOUND, not a console reading: priced at
    # qwen3.7-plus's input/output rate (a flash tier cannot cost MORE than the
    # plus tier) with NO cache discount and NO offpeak=True, so every axis errs
    # toward over-counting. It is here because the model was served, unpriced,
    # and therefore read as FREE — and an unpriced head is the one thing that
    # blinds the cost cap, which is now the binding constraint on the window.
    # REPLACE with the console figure when available; it will only ever go DOWN.
    'qwen3.8-flash':          dict(inp=0.40, out=1.60, cached=0.40),
    'qwen3.6-flash':          dict(inp=0.25, out=1.50, cached=0.25),
    'qwen3.6-flash-2026-04-16': dict(inp=0.25, out=1.50, cached=0.25),
    'qwen3.7-max':            dict(inp=2.50, out=7.50, cached=0.50),
    'qwen3.8-max':            dict(inp=2.00, out=6.00, cached=0.25),
    'qwen3.8-2.4t-a95b':      dict(inp=2.00, out=6.00, cached=0.25),
    'glm-5.2':                dict(inp=1.40, out=4.40, cached=0.28),
    'glm-5.1':                dict(inp=1.40, out=4.40, cached=0.26),
    'glm-5.2-fast-preview':   dict(inp=2.80, out=8.80, cached=0.56),
    'kimi-k3':                dict(inp=3.00, out=15.00, cached=0.30),
    'kimi-k2.7-code':         dict(inp=0.95, out=4.00, cached=0.19),
}

# DeepSeek first-party off-peak schedule (api-docs.deepseek.com/quick_start/pricing):
# off-peak = HALF of peak, and peak is 01:00-04:00 & 06:00-10:00 UTC, Mon-Fri.
# All other hours (including the whole weekend) are off-peak. This replaces the old
# 22:00-08:00 LOCAL band, which was the Alibaba-era assumption and no longer matches
# how the deepseek: primary is actually billed. `is_offpeak` converts `when` to UTC
# before applying the band, so it is correct regardless of the host's local zone.
_PEAK_WINDOWS = ((1, 4), (6, 10))   # (start, end) hours in UTC, inclusive start


def is_offpeak(when: datetime) -> bool:
    """True unless `when` is inside a DeepSeek PEAK window (UTC, Mon-Fri)."""
    if when is None:
        return True     # unknown time -> assume off-peak (the cheaper, safer default)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)   # naive -> assume already UTC
    utc = when.astimezone(timezone.utc)
    if utc.weekday() >= 5:      # Saturday/Sunday are fully off-peak
        return True
    import datetime as _dt
    return not any(start <= utc.hour < end for start, end in _PEAK_WINDOWS)


# Kept as module constants for back-compat with code/tests that referenced the old
# single-band values; they are no longer used by is_offpeak.
OFFPEAK_START, OFFPEAK_END = 22, 8


def price_for(model: str, when: datetime = None):
    """Return (input, output, cached_input) USD per 1M tokens, or None if unknown.

    `model` may carry a provider prefix ('alibaba:x') or a dated suffix; the
    table is keyed on the bare id the gateway reports as `served`.
    """
    if not model:
        return None
    key = str(model).split(':')[-1]
    row = PRICES.get(key)
    if row is None:
        return None
    mult = 0.5 if (row.get('offpeak') and when is not None and is_offpeak(when)) else 1.0
    return row['inp'] * mult, row['out'] * mult, row['cached'] * mult


def cost_of(rec: dict, when: datetime = None):
    """USD for one usage record, or None when the model or counters are unknown.

    Cached input is billed at the cached rate and is NOT double-counted: the
    providers report prompt_tokens as the TOTAL, with cached_tokens a subset.
    """
    price = price_for(rec.get('served') or rec.get('requested'), when)
    if price is None:
        return None
    inp_rate, out_rate, cached_rate = price
    prompt = rec.get('prompt_tokens')
    completion = rec.get('completion_tokens') or 0
    if prompt is None:
        return None
    cached = rec.get('cached_tokens') or 0
    cached = min(cached, prompt)
    uncached = prompt - cached
    return (uncached * inp_rate + cached * cached_rate + completion * out_rate) / 1e6
