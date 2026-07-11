"""Tests for the delayed-entry retry logic in live_test.

When a daily order fires at the 21:00 UTC bar close it collides with OANDA's
maintenance window and is rejected (MARKET_HALTED). The trader keeps the entry
pending and retries on later polls — filling when the market reopens, but only
if price hasn't drifted away (don't chase) and the bar hasn't rolled (stale).
"""
from live_test import entry_retry_decision, order_decision

BAR = "2026-06-18 21:00:00+00:00"
NEXT_BAR = "2026-06-19 21:00:00+00:00"


def _pending(signal=1, price=100.0, atr=2.0, bar=BAR):
    return {'signal': signal, 'signal_price': price, 'atr': atr, 'bar_time': bar}


def test_nothing_pending_waits():
    assert entry_retry_decision(None, BAR, 100.0, 1.0) == 'wait'


def test_bar_rolled_expires():
    # daily signal is stale once a new bar has formed
    assert entry_retry_decision(_pending(), NEXT_BAR, 100.0, 1.0) == 'expire'


def test_no_price_waits():
    assert entry_retry_decision(_pending(), BAR, None, 1.0) == 'wait'


def test_within_drift_retries():
    # price 100.5 vs signal 100.0, atr 2.0 -> 0.25 ATR < 1.0 -> retry
    assert entry_retry_decision(_pending(price=100.0, atr=2.0), BAR, 100.5, 1.0) == 'retry'


def test_beyond_drift_skips():
    # price 103.0 vs 100.0, atr 2.0 -> 1.5 ATR > 1.0 -> skip (don't chase)
    assert entry_retry_decision(_pending(price=100.0, atr=2.0), BAR, 103.0, 1.0) == 'skip'


def test_drift_is_symmetric():
    # a move further into the extreme is also capped by magnitude
    assert entry_retry_decision(_pending(price=100.0, atr=2.0), BAR, 97.0, 1.0) == 'skip'


def test_drift_boundary_is_inclusive_retry():
    # exactly at the cap (1.0 ATR) is not "> cap" -> still retry
    assert entry_retry_decision(_pending(price=100.0, atr=2.0), BAR, 102.0, 1.0) == 'retry'


def test_missing_atr_does_not_block_retry():
    # if ATR is unavailable we cannot bound drift -> don't skip, just retry
    assert entry_retry_decision(_pending(atr=None), BAR, 999.0, 1.0) == 'retry'


def test_tighter_cap_skips_more():
    p = _pending(price=100.0, atr=2.0)
    assert entry_retry_decision(p, BAR, 101.2, 1.0) == 'retry'   # 0.6 ATR
    assert entry_retry_decision(p, BAR, 101.2, 0.5) == 'skip'    # 0.6 ATR > 0.5


# --- exit retry: signal==0 always retries (no drift/expiry) ---

def test_exit_retries_after_bar_roll():
    # exits must not expire when the bar rolls — position is still open
    assert entry_retry_decision(_pending(signal=0), NEXT_BAR, 100.0, 1.0) == 'retry'


def test_exit_retries_despite_price_drift():
    # exits must not skip on drift — we always want to close
    assert entry_retry_decision(_pending(signal=0, price=100.0, atr=2.0), BAR, 200.0, 1.0) == 'retry'


def test_exit_retries_without_price():
    # exits retry even without a price quote
    assert entry_retry_decision(_pending(signal=0), BAR, None, 1.0) == 'retry'


# --- order_decision still behaves (the flip/align contract the retry builds on) ---

def test_order_decision_flip():
    assert order_decision(1, 0, 0, halted=False, startup_pending=False) == 'flip'


def test_order_decision_startup_align_when_midposition():
    # already signaling, no flip, but flat at startup -> align (the case that
    # would otherwise never open)
    assert order_decision(1, 1, 0, halted=False, startup_pending=True) == 'align'


def test_order_decision_none_persistent_midrun():
    # persistent signal, not startup -> no action (stop-fired-flat is intentional)
    assert order_decision(1, 1, 0, halted=False, startup_pending=False) is None
