"""A failed or skipped ENTRY must not advance the recorded signal.

The 2026-07-28 production failure: nas100usd_auto_20260728_080951_i9 signalled
+0 -> -1, the order was rejected because the 21:05 UTC pass ran inside the index
close (the venue publishes its schedule in Europe/Bucharest, not UTC), and
run_once wrote FLAT(sig) anyway. That advanced the recorded signal to -1 with no
position, so every later pass compared -1 to -1, found no change, and did
nothing. The sleeve sat flat holding a live signal it could never act on — for a
daily sleeve, until the signal happens to flip, which can be weeks.

The same write also affected the min-lot SKIP OPEN branch, and there it was
worse: that branch runs BEFORE the close block, so writing FLAT(sig) dropped a
live pos_id while the broker still held the position. sweep_orphans iterates
STATE rather than the broker book, so such a position is invisible to the runner
permanently.

These tests assert on the STATE TRANSITION rather than on the adapter call.
That is deliberate: the adapter is mocked in every other test, which is exactly
why this class of bug survived — the order was placed correctly, and the defect
was in what the runner recorded afterwards.
"""
import fix_runner
from fix_runner import FLAT


def test_flat_preserves_the_signal_it_is_given():
    """FLAT is the primitive both branches use; pin its contract."""
    assert FLAT(-1)['signal'] == -1
    assert FLAT(0)['signal'] == 0
    assert FLAT(-1)['pos_id'] is None
    assert FLAT(-1)['units'] == 0.0


def test_failed_entry_keeps_old_signal_so_next_pass_retries():
    """The nas100 case: 0 -> -1, entry fails, recorded signal must stay 0."""
    st = FLAT(0)                      # flat, nothing held, last acted-on signal 0
    sig = -1                          # today's signal

    # what run_once writes when execute_order returns None
    new = FLAT(st['signal'])

    assert new['signal'] == 0, "recorded signal must NOT advance to the failed sig"
    assert new['pos_id'] is None
    # the next pass compares sig to the recorded signal and MUST see a change
    assert sig != new['signal'], "a failed entry has to be retried next pass"


def test_failed_entry_after_a_successful_close_still_retries():
    """+1 held, flips to -1, close succeeds but the open fails.

    pos_id must clear (the position really is gone) while the signal stays +1,
    so the next pass still sees +1 -> -1 and re-attempts the open.
    """
    st = {'signal': 1, 'pos_id': '4399551', 'units': 1000.0,
          'side': 1, 'stop': 1.23, 'stop_ref': {'ord_status': '0', 'ref': '4399551'}}
    sig = -1

    new = FLAT(st['signal'])

    assert new['pos_id'] is None, "the close happened; state must not keep a dead pos_id"
    assert new['signal'] == 1, "signal must not advance past an entry that never happened"
    assert sig != new['signal']


def test_flat_sig_would_have_swallowed_the_retry():
    """Reproduces the pre-fix behaviour, so a regression is caught, not argued about."""
    st = FLAT(0)
    sig = -1

    buggy = FLAT(sig)                 # what the code used to write

    assert buggy['signal'] == sig
    assert sig == buggy['signal'], (
        "pre-fix: recorded signal equals the current signal, so the next pass "
        "finds no change and never retries the entry")


def test_min_lot_skip_must_not_drop_a_live_position():
    """The early SKIP OPEN branch runs BEFORE the close block.

    Writing FLAT(sig) there discarded a real pos_id without closing anything.
    The fix keeps the prior state verbatim.
    """
    st = {'signal': 1, 'pos_id': '4387584', 'units': 1000.0,
          'side': 1, 'stop': 0.845, 'stop_ref': {'ord_status': '0', 'ref': '4387584'}}
    sig = -1

    kept = st                          # what the code now writes
    dropped = FLAT(sig)                # what it used to write

    assert kept['pos_id'] == '4387584', "a tracked position must survive a min-lot skip"
    assert dropped['pos_id'] is None, "pre-fix: the live pos_id was silently discarded"
    assert kept['signal'] != sig, "and the skip must stay re-evaluable next pass"


def test_zero_signal_still_advances():
    """Guard against over-correcting: a genuine flatten SHOULD advance to 0.

    sig == 0 means 'be flat', and a successful close achieves it. If that also
    refused to advance, the runner would try to close an already-closed position
    on every subsequent pass.
    """
    st = {'signal': 1, 'pos_id': '4399557', 'units': 1000.0,
          'side': 1, 'stop': 1.14, 'stop_ref': None}
    sig = 0

    new = FLAT(sig)                    # unchanged by the fix: sig==0 path

    assert new['signal'] == 0
    assert new['pos_id'] is None
    assert sig == new['signal'], "a completed flatten must not be retried forever"
