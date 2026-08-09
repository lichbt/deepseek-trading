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

    def test_direct_models_not_pinned(self):
        assert ar._provider_for_model(ar.THESIS_MODEL) is None
        assert ar._provider_for_model(ar.THESIS_FALLBACK) is None
        assert ar._provider_for_model(ar.THESIS_FINAL_FALLBACK) is None
        assert ar._provider_for_model(ar.SELF_CRITIQUE_MODEL) is None


class TestDirectProviderRoutes:
    def test_cline_route_strips_prefix(self, monkeypatch):
        monkeypatch.setattr(ar, "CLINE_BASE", "https://cline.example/v1")
        monkeypatch.setattr(ar, "CLINE_KEY", "token")
        assert ar._route_model("cline:code-model") == (
            "https://cline.example/v1", "token", "code-model", True)

    def test_opencode_route_strips_prefix(self, monkeypatch):
        monkeypatch.setattr(ar, "OPENCODE_BASE", "https://opencode.example/v1")
        monkeypatch.setattr(ar, "OPENCODE_KEY", "token")
        assert ar._route_model("opencode:glm-5.2") == (
            "https://opencode.example/v1", "token", "glm-5.2", True)

    def test_codegen_fallback_is_explicitly_routed_and_spans_two_providers(self):
        """The head is explicitly routed; a SECOND provider must back it up.

        Pinning an exact index broke when ninerouter was dropped, and pinning the
        LEAD PROVIDER broke the same way when the chains moved from opencode to
        byteplus (2026-08) — which provider leads is deployment config, not code.
        Two durable properties survive that: the head carries an explicit provider
        prefix (generation is deliberately routed, never silently defaulted to
        OpenRouter), and the chain spans more than one provider so it can survive
        an outage.
        """
        head = ar.CODE_FALLBACK_MODELS[0]
        assert ar._provider_of(head).endswith(':'), f'head is not explicitly routed: {head}'
        providers = {ar._provider_of(m) for m in ar.CODE_FALLBACK_MODELS}
        assert len(providers) >= 2, ar.CODE_FALLBACK_MODELS


class TestParseModelChain:
    """_parse_model_chain turns comma-separated route IDs
    (THESIS_MODELS=cline:foo,byteplus:bar,ninerouter:thesis) into a clean list,
    falling back to the default when unset/effectively empty. Stdlib only."""
    ENV = 'THESIS_MODELS'

    def test_unset_returns_default_copy(self, monkeypatch):
        monkeypatch.delenv(self.ENV, raising=False)
        dflt = ['byteplus:a', 'ninerouter:b']
        out = ar._parse_model_chain(self.ENV, dflt)
        assert out == dflt
        assert out is not dflt  # always a fresh list, never the caller's object

    def test_empty_or_whitespace_returns_default(self, monkeypatch):
        for raw in ('', '   ', '\t'):
            monkeypatch.setenv(self.ENV, raw)
            assert ar._parse_model_chain(self.ENV, ['byteplus:a']) == ['byteplus:a']

    def test_trims_whitespace_per_entry(self, monkeypatch):
        monkeypatch.setenv(self.ENV, '  byteplus:a , ninerouter:b ,cline:c  ')
        assert ar._parse_model_chain(self.ENV, []) == [
            'byteplus:a', 'ninerouter:b', 'cline:c']

    def test_drops_empty_entries(self, monkeypatch):
        monkeypatch.setenv(self.ENV, 'byteplus:a,, ,ninerouter:b')
        assert ar._parse_model_chain(self.ENV, []) == [
            'byteplus:a', 'ninerouter:b']

    def test_drops_unresolved_prefix_tokens(self, monkeypatch):
        # a bare `cline:` (prefix with no model after it) is skipped
        monkeypatch.setenv(self.ENV, 'byteplus:a,cline:,ninerouter:b')
        assert ar._parse_model_chain(self.ENV, []) == [
            'byteplus:a', 'ninerouter:b']

    def test_all_filtered_falls_back_to_default(self, monkeypatch):
        # never an empty chain — a misconfigured env falls back to the default
        monkeypatch.setenv(self.ENV, 'cline:,,:')
        dflt = ['byteplus:a', 'ninerouter:b']
        assert ar._parse_model_chain(self.ENV, dflt) == dflt

    def test_env_override_preserves_order(self, monkeypatch):
        monkeypatch.setenv(self.ENV, 'ninerouter:z,byteplus:a,cline:c')
        assert ar._parse_model_chain(self.ENV, []) == [
            'ninerouter:z', 'byteplus:a', 'cline:c']


class TestModelChainWiring:
    """The live module wires each *_MODELS chain through _parse_model_chain, and
    the legacy single-model aliases are derived from the chain head/tail."""

    def test_thesis_chain_wired_to_thesis_models_env(self):
        assert ar.THESIS_MODELS == ar._parse_model_chain(
            'THESIS_MODELS', ar._DEFAULT_THESIS_MODELS)

    def test_critique_chain_wired_to_critique_models_env(self):
        assert ar.CRITIQUE_MODELS == ar._parse_model_chain(
            'CRITIQUE_MODELS', ar._DEFAULT_CRITIQUE_MODELS)
        assert ar.SELF_CRITIQUE_MODELS is ar.CRITIQUE_MODELS

    def test_codegen_chain_wired_to_codegen_models_env(self):
        assert ar.CODEGEN_MODELS == ar._parse_model_chain(
            'CODEGEN_MODELS', ar._DEFAULT_CODEGEN_MODELS)
        assert ar.CODE_FALLBACK_MODELS is ar.CODEGEN_MODELS

    def test_legacy_aliases_derived_from_chain(self):
        assert ar.THESIS_MODEL == ar.THESIS_MODELS[0]
        assert ar.THESIS_FALLBACK == (
            ar.THESIS_MODELS[1] if len(ar.THESIS_MODELS) > 1 else ar.THESIS_MODELS[0])
        assert ar.THESIS_FINAL_FALLBACK == ar.THESIS_MODELS[-1]
        assert ar.SELF_CRITIQUE_MODEL == ar.CRITIQUE_MODELS[0]

    def test_chains_never_empty(self):
        for label, chain in (('thesis', ar.THESIS_MODELS),
                             ('critique', ar.CRITIQUE_MODELS),
                             ('codegen', ar.CODEGEN_MODELS),
                             ('default_thesis', ar._DEFAULT_THESIS_MODELS)):
            assert chain, f'{label} chain must never be empty'

    def test_live_env_chains_match_current_routing(self):
        """Every chain is explicitly routed and spans two providers.

        The second provider is what makes the circuit breaker meaningful: with a
        single-provider chain there is nothing to fail over TO, so an outage
        takes the whole stage down.

        This used to assert `opencode:` led every chain. That is DEPLOYMENT
        config, not code — the chains moved to byteplus in 2026-08 and the
        assertion went stale, exactly the failure commit 4a58d69 fixed elsewhere.
        The multi-provider property is NOT stale and is kept: it is currently
        failing for CODEGEN_MODELS, which is a real gap in the deployed .env
        rather than a stale test.
        """
        for name in ('THESIS_MODELS', 'CRITIQUE_MODELS', 'CODEGEN_MODELS'):
            chain = getattr(ar, name)
            assert ar._provider_of(chain[0]).endswith(':'), \
                f'{name} head is not explicitly routed: {chain}'
            providers = {ar._provider_of(m) for m in chain}
            assert len(providers) >= 2, f'{name} is single-provider: {chain}'
