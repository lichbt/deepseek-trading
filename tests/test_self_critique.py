"""Tests for auto_research.self_critique_thesis — the design-quality reflection
gate that runs between thesis validation and code-gen.

Contract: returns {'verdict': 'pass'|'reject', 'reason': str}; rejects only on
an explicit 'reject' verdict; ALWAYS fails open (any LLM/parse/exception path
yields 'pass') so a flaky API never starves the research batch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar


THESIS = {
    "strategy_family": "regime", "timeframe": "D",
    "rationale": "Mean reversion after volatility spikes.",
    "entry_condition": "close < lower Bollinger band",
    "filter_condition": "ATR(14) > its 50-bar median",
    "exit_condition": "exit after 3 bars",
}


def _caller(responses):
    """Return a fake _call that yields queued responses in order and records calls."""
    calls = []
    seq = list(responses)

    def fake(**kwargs):
        calls.append(kwargs)
        return seq.pop(0) if seq else {"success": False, "candidate": None, "error": "no more"}

    fake.calls = calls
    return fake


def _ok(candidate):
    return {"success": True, "candidate": candidate, "error": None}


def _fail(msg="boom"):
    return {"success": False, "candidate": None, "error": msg}


class TestSelfCritique:
    def test_reject_is_returned(self):
        call = _caller([_ok({"verdict": "reject", "reason": "entry rides breakouts but rationale claims reversal"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert "breakouts" in out["reason"]
        assert len(call.calls) == 1                      # no fallback needed on success

    def test_pass_is_returned(self):
        call = _caller([_ok({"verdict": "pass", "reason": "coherent"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"

    def test_unknown_verdict_defaults_to_pass(self):
        """Anything that isn't an explicit 'reject' must pass (conservative)."""
        call = _caller([_ok({"verdict": "maybe", "reason": "unsure"})])
        assert ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)["verdict"] == "pass"

    def test_missing_verdict_defaults_to_pass(self):
        call = _caller([_ok({"reason": "no verdict key"})])
        assert ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)["verdict"] == "pass"

    def test_both_calls_fail_is_fail_open_pass(self):
        call = _caller([_fail("primary down"), _fail("fallback down")])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert "fail-open" in out["reason"]
        assert len(call.calls) == 2                      # tried primary then fallback

    def test_fallback_used_when_primary_fails(self):
        call = _caller([_fail("primary down"),
                        _ok({"verdict": "reject", "reason": "circular regime gate"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert call.calls[0]["model"] == ar.THESIS_MODEL
        assert call.calls[1]["model"] == ar.THESIS_FALLBACK

    def test_non_dict_candidate_fails_open(self):
        call = _caller([_ok(["not", "a", "dict"])])
        assert ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)["verdict"] == "pass"

    def test_none_candidate_fails_open(self):
        call = _caller([_ok(None)])
        assert ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)["verdict"] == "pass"

    def test_exception_in_call_fails_open(self):
        def boom(**kwargs):
            raise RuntimeError("network exploded")
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=boom)
        assert out["verdict"] == "pass"
        assert "fail-open" in out["reason"]

    def test_thesis_fields_reach_the_prompt(self):
        call = _caller([_ok({"verdict": "pass", "reason": ""})])
        ar.self_critique_thesis(THESIS, "GBP_JPY", _call=call)
        user = call.calls[0]["user_prompt"]
        assert "GBP_JPY" in user
        assert "Mean reversion after volatility spikes." in user
        assert "lower Bollinger band" in user            # entry reaches the critic
        assert "ATR(14)" in user                          # filter reaches the critic

    def test_reject_with_empty_reason_still_rejects(self):
        call = _caller([_ok({"verdict": "reject", "reason": ""})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert out["reason"]                              # gets a placeholder reason
