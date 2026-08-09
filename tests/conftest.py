"""Shared test isolation.

The provider circuit breaker (`auto_research._PROVIDER_HEALTH`) is deliberately
PROCESS-GLOBAL and survives for a 300s cooldown — correct in production, where a
batch should pay for an outage once rather than on every call. In a test session
it means one test that trips a provider silently reorders every later chain.

That is not hypothetical: it made three tests in test_self_critique.py fail in a
full-suite run while passing in isolation. `self_critique_thesis` correctly called
the demoted chain's new head (`ninerouter:thesis`) while the assertion compared
against the static alias `SELF_CRITIQUE_MODEL` (`byteplus:deepseek-v4-flash`), so
the failure looked like a routing bug and was really leaked state.

Reset it around every test so breaker state is never inherited.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_provider_breaker():
    try:
        import auto_research as ar
    except Exception:            # a test env without the module — nothing to reset
        yield
        return
    saved = dict(getattr(ar, '_PROVIDER_HEALTH', {}))
    ar._PROVIDER_HEALTH.clear()
    yield
    ar._PROVIDER_HEALTH.clear()
    ar._PROVIDER_HEALTH.update(saved)
