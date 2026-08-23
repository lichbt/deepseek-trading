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
from datetime import datetime

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
    'deepseek-v4-pro-0813':   dict(inp=1.32, out=3.96, cached=0.132, offpeak=True),
    'deepseek-v4-flash-0731': dict(inp=0.44, out=1.32, cached=0.044, offpeak=True),
    'deepseek-v4-pro':        dict(inp=2.40, out=4.80, cached=0.20),
    'qwen3.7-plus':           dict(inp=0.40, out=1.60, cached=0.08),
    # The qwen flash tiers are priced by PROMPT LENGTH. Both entries below use
    # the SHORT-context tier, which is safe here because _call_openrouter_once
    # hard-refuses any prompt over 12,000 tokens — this pipeline can never reach
    # the 32K boundary where qwen3.7-flash jumps to $0.10/$0.40.
    'qwen3.7-flash':          dict(inp=0.030, out=0.130, cached=0.030),
    'qwen3.7-flash-2026-07-15': dict(inp=0.030, out=0.130, cached=0.030),
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

# The console states the discount as "between 22:00 and 08:00" and does NOT name
# a timezone. These bounds are applied to the record's LOCAL hour, which is the
# same assumption RESEARCH_WINDOW makes. If a night's billed credits do not come
# out at ~half, this is the first thing to suspect.
OFFPEAK_START, OFFPEAK_END = 22, 8


def is_offpeak(when: datetime) -> bool:
    """True inside the discounted night band (wraps midnight)."""
    return when.hour >= OFFPEAK_START or when.hour < OFFPEAK_END


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
