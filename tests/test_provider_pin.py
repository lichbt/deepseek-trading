"""Tests for _provider_for_model — the OpenRouter provider pin that lands paid
first-party DeepSeek calls on a consistent provider so its prompt cache hits
across batches, without breaking :free variants or non-deepseek models."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import auto_research as ar


class TestProviderForModel:
    def test_paid_deepseek_is_pinned_with_fallbacks_on(self):
        p = ar._provider_for_model('deepseek/deepseek-v4-flash')
        assert p == {'order': ['deepseek'], 'allow_fallbacks': True}
        # allow_fallbacks MUST stay True — pinning must not remove resilience
        assert p['allow_fallbacks'] is True

    def test_paid_deepseek_chat_is_pinned(self):
        assert ar._provider_for_model('deepseek/deepseek-chat') is not None

    def test_free_deepseek_variant_not_pinned(self):
        # ':free' routes to third-party free hosts — pinning 'deepseek' would break it
        assert ar._provider_for_model('deepseek/deepseek-v4-flash:free') is None

    def test_non_deepseek_not_pinned(self):
        assert ar._provider_for_model('openai/gpt-oss-120b:free') is None
        assert ar._provider_for_model('meta-llama/llama-3.3-70b-instruct:free') is None

    def test_thesis_model_pinned_critique_model_not(self):
        # the live wiring: thesis-gen (paid deepseek) caches; self-critique (free) doesn't
        assert ar._provider_for_model(ar.THESIS_MODEL) is not None
        assert ar._provider_for_model(ar.SELF_CRITIQUE_MODEL) is None
