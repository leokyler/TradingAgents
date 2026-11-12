"""OpenCode Go: OpenAI-compatible subscription with two extra request headers.

OpenCode's docs ask clients to identify themselves by name and to send a stable
``x-opencode-session`` per conversation (routing and prompt-cache affinity), on
top of the bearer token a standard OpenAI client already sends.
"""

import pytest

from tradingagents.llm_clients import openai_client
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import (
    OPENAI_COMPATIBLE_PROVIDERS,
    LocalCompatibleChatOpenAI,
    OpenAIClient,
    _opencode_go_headers,
    _opencode_session_id,
)
from tradingagents.llm_clients.validators import validate_model

BASE_URL = "https://opencode.ai/inference/go/openai/v1"


@pytest.fixture(autouse=True)
def _fresh_session(monkeypatch):
    """Each test starts with no generated session id and no pinned override."""
    monkeypatch.setattr(openai_client, "_OPENCODE_SESSION_ID", None)
    monkeypatch.delenv("OPENCODE_GO_SESSION_ID", raising=False)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "oc_sk_test")
    yield


@pytest.mark.unit
def test_provider_is_registered_as_openai_compatible():
    spec = OPENAI_COMPATIBLE_PROVIDERS["opencode-go"]
    assert spec.base_url == BASE_URL
    assert spec.headers is _opencode_go_headers
    assert spec.chat_class is LocalCompatibleChatOpenAI
    assert create_llm_client(provider="opencode-go", model="glm-5.3").provider == "opencode-go"


@pytest.mark.unit
def test_api_key_comes_from_the_documented_env_var():
    assert get_api_key_env("opencode-go") == "OPENCODE_GO_API_KEY"


@pytest.mark.unit
def test_missing_key_is_named_in_the_error(monkeypatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENCODE_GO_API_KEY"):
        OpenAIClient(model="glm-5.3", provider="opencode-go").get_llm()


@pytest.mark.unit
def test_client_carries_the_required_headers():
    llm = OpenAIClient(model="glm-5.3", provider="opencode-go").get_llm()

    assert isinstance(llm, LocalCompatibleChatOpenAI)
    assert str(llm.openai_api_base).rstrip("/") == BASE_URL
    headers = llm.default_headers
    assert headers["x-opencode-session"] == _opencode_session_id()
    assert headers["User-Agent"].startswith("TradingAgents/")


@pytest.mark.unit
def test_session_id_is_stable_within_a_process():
    first = OpenAIClient(model="glm-5.3", provider="opencode-go").get_llm()
    second = OpenAIClient(model="kimi-k2.7-code", provider="opencode-go").get_llm()

    assert (first.default_headers["x-opencode-session"]
            == second.default_headers["x-opencode-session"])


@pytest.mark.unit
def test_env_pins_the_session_id_across_processes(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_SESSION_ID", "conversation-42")
    assert _opencode_session_id() == "conversation-42"
    assert _opencode_go_headers()["x-opencode-session"] == "conversation-42"


@pytest.mark.unit
def test_custom_backend_url_is_honored():
    llm = OpenAIClient(
        model="glm-5.3", provider="opencode-go",
        base_url="https://opencode.ai/zen/go/v1",
    ).get_llm()
    assert str(llm.openai_api_base).rstrip("/") == "https://opencode.ai/zen/go/v1"


@pytest.mark.unit
def test_gateway_models_are_accepted_without_a_fixed_list():
    # The gateway's catalog changes; a model id it serves must not be rejected.
    assert validate_model("opencode-go", "some-future-model")


@pytest.mark.unit
def test_cli_offers_the_provider_and_its_models():
    from cli.prompts import provider_default_url

    assert provider_default_url("opencode-go") == BASE_URL
    quick = {value for _, value in get_model_options("opencode-go", "quick")}
    deep = {value for _, value in get_model_options("opencode-go", "deep")}
    assert "custom" in quick and "custom" in deep
    assert "glm-5.3-flash" in quick
    assert "deepseek-v4-pro" in deep
