"""Reasoning effort per model tier.

The quick tier serves the analysts, the debate, the trader and the risk team; the
deep tier serves the two managers. Effort is a provider parameter, so a tier that
names none keeps the provider-wide ``openai_reasoning_effort`` and, when that is
unset too, sends nothing at all so the model runs at its own default.
"""

import pytest

from tradingagents.default_config import build_default_config
from tradingagents.llm_clients import factory
from tradingagents.llm_clients.openai_client import OpenAIClient


def _config(**overrides):
    config = {
        "llm_provider": "opencode-go",
        "quick_think_llm": "deepseek-v4.1-flash",
        "deep_think_llm": "deepseek-v4.1-flash",
        "quick_think_provider": None,
        "deep_think_provider": None,
        "quick_think_reasoning_effort": None,
        "deep_think_reasoning_effort": None,
        "openai_reasoning_effort": None,
    }
    config.update(overrides)
    return config


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "oc_sk_test")
    yield


def _effort(config, tier):
    return getattr(factory.create_tier_client(config, tier).get_llm(), "reasoning_effort", None)


@pytest.mark.unit
def test_unset_tier_sends_no_effort_and_uses_the_model_default(monkeypatch):
    config = _config(deep_think_reasoning_effort="max")
    assert _effort(config, "quick") is None
    assert _effort(config, "deep") == "max"


@pytest.mark.unit
def test_both_tiers_can_differ():
    config = _config(quick_think_reasoning_effort="low", deep_think_reasoning_effort="max")
    assert _effort(config, "quick") == "low"
    assert _effort(config, "deep") == "max"


@pytest.mark.unit
def test_tier_falls_back_to_the_provider_wide_value():
    config = _config(openai_reasoning_effort="high")
    assert _effort(config, "quick") == "high"
    assert _effort(config, "deep") == "high"


@pytest.mark.unit
def test_tier_value_wins_over_the_provider_wide_one():
    config = _config(openai_reasoning_effort="high", deep_think_reasoning_effort="max")
    assert _effort(config, "quick") == "high"
    assert _effort(config, "deep") == "max"


@pytest.mark.unit
def test_gateway_models_keep_their_effort():
    """The native-OpenAI model-name gate must not run for a compatible gateway."""
    llm = OpenAIClient(model="deepseek-v4.1-flash", provider="opencode-go",
                       reasoning_effort="max").get_llm()
    assert llm.reasoning_effort == "max"


@pytest.mark.unit
def test_native_openai_still_drops_effort_for_non_reasoning_models(monkeypatch):
    """gpt-4.1 would 400 with the parameter, so the gate still applies on OpenAI."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    dropped = OpenAIClient(model="gpt-4.1", provider="openai", reasoning_effort="high").get_llm()
    kept = OpenAIClient(model="gpt-6-sol", provider="openai", reasoning_effort="high").get_llm()
    assert dropped.reasoning_effort is None
    assert kept.reasoning_effort == "high"


@pytest.mark.unit
def test_a_provider_outside_the_list_sends_no_effort(monkeypatch):
    """An endpoint that does not know the field must not receive it.

    The whitelist lives in ``build_llm_kwargs``, the path a run takes; a caller
    constructing ``OpenAIClient(..., reasoning_effort=...)`` directly is asking
    for it explicitly and is not gated.
    """
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    config = _config(llm_provider="glm", quick_think_reasoning_effort="high")
    assert _effort(config, "quick") is None


@pytest.mark.unit
@pytest.mark.parametrize("env_var, key", [
    ("TRADINGAGENTS_QUICK_THINK_EFFORT", "quick_think_reasoning_effort"),
    ("TRADINGAGENTS_DEEP_THINK_EFFORT", "deep_think_reasoning_effort"),
])
def test_env_overrides_are_wired(monkeypatch, env_var, key):
    monkeypatch.setenv(env_var, "max")
    assert build_default_config()[key] == "max"
