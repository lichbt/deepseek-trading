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

    def test_all_calls_fail_is_fail_open_pass(self):
        call = _caller([_fail("provider down")] * len(ar.SELF_CRITIQUE_MODELS))
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert "fail-open" in out["reason"]
        assert len(call.calls) == len(ar.SELF_CRITIQUE_MODELS)

    def test_critique_falls_back_in_order(self):
        call = _caller([_fail(), _ok({"verdict": "pass", "reason": "coherent"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert [c["model"] for c in call.calls] == ar.SELF_CRITIQUE_MODELS[:2]

    def test_call_uses_self_critique_model(self):
        call = _caller([_ok({"verdict": "reject", "reason": "circular regime gate"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert call.calls[0]["model"] == ar.SELF_CRITIQUE_MODEL

    def test_post_codegen_rejects_clear_mismatch(self):
        call = _caller([_ok({"verdict": "reject", "reason": "invented autocorrelation gate"})])
        out = ar.post_codegen_fidelity_critique(
            THESIS, {"code": "def generate_signals(df, params): pass", "param_grid": {"n": [10]}},
            "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert "autocorrelation" in out["reason"]
        assert call.calls[0]["model"] == ar.SELF_CRITIQUE_MODEL

    def test_post_codegen_passes_aligned_code(self):
        call = _caller([_ok({"verdict": "pass", "reason": "aligned"})])
        out = ar.post_codegen_fidelity_critique(
            THESIS, {"code": "def generate_signals(df, params): pass", "param_grid": {"n": [10]}},
            "EUR_USD", _call=call)
        assert out["verdict"] == "pass"

    def test_post_codegen_fails_open(self):
        call = _caller([_fail("provider down")])
        out = ar.post_codegen_fidelity_critique(THESIS, {}, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert "fail-open" in out["reason"]

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


class TestCritiquePromptNotOverAggressive:
    """Regression: the gate was rejecting the system's OWN mandated regime
    detectors (autocorrelation, ATR/vol regime, efficiency ratio, MA-slope) as
    'circular' merely for sharing the price series with the entry — a 67%
    reject rate that starved the funnel (2026-06-14). The prompt must bless
    those as independent and define circular narrowly."""

    def test_regime_detectors_blessed_as_independent(self):
        sysp = ar._SELF_CRITIQUE_SYSTEM.lower()
        for token in ('autocorrelation', 'efficiency ratio', 'volatility',
                      'ma-slope', 'not circular by itself'):
            assert token in sysp, f"prompt missing exemption token: {token!r}"

    def test_circular_defined_narrowly_as_same_condition(self):
        # Must anchor 'circular' to restating an ENTRY CONDITION, not to sharing
        # the price series. v4 (shipped 2026-08-21, commit 37bfdcd) replaced v3's
        # prose "same condition" with an explicit mechanical redundancy list, so
        # assert that list's anchors — the guardrail got STRONGER, the wording moved.
        sysp = ar._SELF_CRITIQUE_SYSTEM.lower()
        assert 'identical to an entry condition' in sysp
        assert 'embedded inside the entry' in sysp
        assert 'not circular by itself' in sysp

    def test_completed_bar_entry_not_lookahead(self):
        sysp = ar._SELF_CRITIQUE_SYSTEM.lower()
        assert 'completed bar' in sysp and 'next bar' in sysp

    def test_positive_shift_is_not_lookahead(self):
        # Regression (2026-06-14): the gate hallucinated look-ahead on
        # `SMA(50).shift(10)` twice in one batch — a POSITIVE shift is a PAST value.
        sysp = ar._SELF_CRITIQUE_SYSTEM.lower()
        assert 'shift' in sysp and 'past value' in sysp
        assert 'negative shift' in sysp        # must keep the real look-ahead case

    def test_redundant_means_literal_not_assumed_regime(self):
        # Regression: autocorrelation/efficiency-ratio gates were rejected as
        # 'redundant' for confirming the regime the entry's rationale assumes.
        sysp = ar._SELF_CRITIQUE_SYSTEM.lower()
        # v4 states this as an OBJECTIVE TEXTUAL TEST over the entry's own
        # conditions; v3 said "literal condition". Same rule, mechanical form.
        assert 'objective textual test' in sysp
        assert 'rationale merely assumes' in sysp

    def test_critique_runs_at_temperature_zero(self):
        # Binary judgment gate -> greedy decoding for the most-likely verdict.
        assert ar.SELF_CRITIQUE_TEMPERATURE == 0.0


class TestFailOpenIsVisible:
    """A fail-open passes a candidate to code-gen UNJUDGED.

    It used to be indistinguishable from a real pass — same return shape, and the
    caller printed the same "✓ Self-critique passed" tick. That makes any
    reject-rate reading over-state how much judging actually happened.
    """

    def test_real_pass_is_not_flagged(self):
        call = _caller([_ok({"verdict": "pass", "reason": "fine"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert not out.get("failed_open")

    def test_llm_failure_is_flagged(self):
        call = _caller([_fail("Empty content from model (finish_reason=length)")] * 8)
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "pass"
        assert out["failed_open"] is True

    def test_non_dict_candidate_is_flagged(self):
        call = _caller([_ok(["not", "a", "dict"])])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["failed_open"] is True

    def test_exception_is_flagged(self):
        def boom(**kwargs):
            raise RuntimeError('network gone')
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=boom)
        assert out["verdict"] == "pass"
        assert out["failed_open"] is True

    def test_a_reject_is_never_flagged(self):
        call = _caller([_ok({"verdict": "reject", "reason": "circular gate"})])
        out = ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert out["verdict"] == "reject"
        assert not out.get("failed_open")


class TestOnlyThePrimaryHeadMayReject:
    """A fallback judge may PASS, never REJECT (2026-09-16).

    The chain exists so a provider outage cannot stop judging, but the heads are
    not interchangeable: over 2026-09-11..09-15 the configured heads ran 0.8%
    (deepseek-flash) vs 11.9% (qwen3.8-flash) FIDELITY rejects on the same
    generation distribution, and 60 of those rejects re-judged by the other head
    reproduced only 53% (while rejecting 3% of 60 known passes). A false reject
    destroys a thesis and starves generation silently, which is this gate's
    documented failure mode, so a reject from anywhere but the configured head is
    recorded and ignored.
    """

    def test_fallback_reject_is_downgraded_to_pass(self, monkeypatch):
        monkeypatch.setattr(ar, 'SELF_CRITIQUE_MODELS', ['primary:model', 'fallback:model'])
        call = _caller([_fail('primary down'), _ok({'verdict': 'reject', 'reason': 'entry rides breakouts'})])
        out = ar.self_critique_thesis(THESIS, 'EUR_USD', _call=call)
        assert out['verdict'] == 'pass'
        assert out['fallback_reject_ignored'] is True
        assert not out.get('failed_open')                    # judged, just out-voted
        assert out['served_by'] == 'fallback:model'
        assert 'ignored' in out['reason'] and 'breakouts' in out['reason']
        assert [c['model'] for c in call.calls] == ['primary:model', 'fallback:model']

    def test_primary_reject_still_rejects(self, monkeypatch):
        monkeypatch.setattr(ar, 'SELF_CRITIQUE_MODELS', ['primary:model', 'fallback:model'])
        call = _caller([_ok({'verdict': 'reject', 'reason': 'circular gate'})])
        out = ar.self_critique_thesis(THESIS, 'EUR_USD', _call=call)
        assert out['verdict'] == 'reject'
        assert out['served_by'] == 'primary:model'
        assert not out.get('fallback_reject_ignored')

    def test_health_demotion_does_not_move_the_reject_policy(self, monkeypatch):
        """_chain_order demotes a provider whose API just tripped, so attempt
        order is NOT the policy: if the configured primary is asked second and
        serves the reject, it still counts."""
        monkeypatch.setattr(ar, 'SELF_CRITIQUE_MODELS', ['primary:model', 'fallback:model'])
        monkeypatch.setattr(ar, '_chain_order', lambda m: list(reversed(list(m))))
        call = _caller([_ok({'verdict': 'reject', 'reason': 'x' * 10})])   # first asked = fallback
        out = ar.self_critique_thesis(THESIS, 'EUR_USD', _call=call)
        assert [c['model'] for c in call.calls] == ['fallback:model']
        assert out['verdict'] == 'pass' and out['fallback_reject_ignored'] is True

    def test_single_head_still_rejects(self):
        """critique_replay.py sets a ONE-model chain per arm; the rule must not
        make a replay arm unable to reject (that would invalidate the arm)."""
        call = _caller([_ok({'verdict': 'reject', 'reason': 'invented gate'})])
        assert ar.self_critique_thesis(THESIS, 'EUR_USD', _call=call)['verdict'] == 'reject'

    def test_post_codegen_fallback_reject_is_downgraded(self, monkeypatch):
        monkeypatch.setattr(ar, 'SELF_CRITIQUE_MODELS', ['primary:model', 'fallback:model'])
        call = _caller([_fail('primary down'), _ok({'verdict': 'reject', 'reason': 'invented autocorrelation gate'})])
        out = ar.post_codegen_fidelity_critique(
            THESIS, {'code': 'def generate_signals(df, params): pass', 'param_grid': {'n': [10]}},
            'EUR_USD', _call=call)
        assert out['verdict'] == 'pass'
        assert out['fallback_reject_ignored'] is True
        assert out['served_by'] == 'fallback:model'

    def test_served_by_is_recorded_in_the_corpus(self, monkeypatch, tmp_path):
        """The corpus recorded only the CONFIGURED head, so a per-head analysis
        silently mixed two judges whenever the chain fell through."""
        import json
        log = tmp_path / 'critique_theses.jsonl'
        monkeypatch.setattr(ar, '_CRITIQUE_LOG', str(log))
        monkeypatch.setattr(ar, 'SELF_CRITIQUE_MODELS', ['primary:model', 'fallback:model'])
        call = _caller([_fail('primary down'), _ok({'verdict': 'reject', 'reason': 'meh'})])
        out = ar.self_critique_thesis(THESIS, 'EUR_USD', _call=call)
        ar._record_critique(THESIS, 'EUR_USD', out)
        row = json.loads(log.read_text().strip())
        assert row['critique_head'] == 'primary:model'          # configured
        assert row['critique_served_by'] == 'fallback:model'    # served
        assert row['fallback_reject_ignored'] is True


    def test_codegen_fidelity_log_records_the_code_and_never_raises(self, monkeypatch, tmp_path):
        """The only fidelity verdict formed by a judge that READ the code was
        booked as results['errors'] until 2026-09-16, so it could not be counted
        or read back. Log rejections only (passepasse ~8/day of them)."""
        import json
        log = tmp_path / 'codegen_fidelity.jsonl'
        monkeypatch.setattr(ar, '_CODEGEN_FIDELITY_LOG', str(log))
        ar._record_codegen_fidelity(
            THESIS, {'code': 'def generate_signals(df, params): return df',
                     'param_grid': {'n': [10]}},
            'EUR_USD', {'verdict': 'reject', 'reason': 'invented gate',
                        'served_by': 'primary:model'}, 'retry', repaired=False)
        row = json.loads(log.read_text().strip())
        assert row['stage'] == 'retry' and row['repaired'] is False
        assert row['code'].startswith('def generate_signals')
        assert row['instrument'] == 'EUR_USD' and row['reason'] == 'invented gate'
        # a non-dict candidate (the retry-error path) must not raise
        ar._record_codegen_fidelity(THESIS, None, 'EUR_USD', {'reason': 'API 429'}, 'retry_error')
        assert len(log.read_text().strip().splitlines()) == 2


class TestCritiqueTokenBudget:
    """The 2000-token budget exists for models whose reasoning cannot be turned
    off. Where it CAN (alibaba sends enable_thinking:false), that headroom only
    lets a rambling model burn 2000 tokens before returning empty content."""

    def test_thinking_off_models_get_the_small_budget(self):
        assert ar._critique_max_tokens('alibaba:qwen3.7-plus') == ar.SELF_CRITIQUE_MAX_TOKENS_NO_THINK
        assert ar._critique_max_tokens('alibaba:qwen3.6-flash') < 1000

    def test_other_providers_keep_the_reasoning_headroom(self):
        # Measured 2026-07-23: minimax-m3 fails this gate at 400, passes at 2000.
        assert ar._critique_max_tokens('byteplus:glm-5.2') == ar.SELF_CRITIQUE_MAX_TOKENS
        # ninerouter legs are thinking-off too (see _deepseek_thinking_off) — the
        # 9router Codegen combo serves DeepSeek-flash and honours the toggle.
        assert ar._critique_max_tokens('ninerouter:codegen') == ar.SELF_CRITIQUE_MAX_TOKENS_NO_THINK

    def test_budget_actually_reaches_the_call(self):
        call = _caller([_ok({"verdict": "pass", "reason": "ok"})])
        ar.self_critique_thesis(THESIS, "EUR_USD", _call=call)
        assert call.calls[0]['max_tokens'] == ar._critique_max_tokens(call.calls[0]['model'])
