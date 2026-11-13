"""OpenCode Go: an OpenAI-compatible subscription gateway over other vendors' models.

The gateway speaks the OpenAI API, so it needs nothing special to be *called*;
what it wants beyond the bearer token is what this module owns:

* a client identity — its docs ask clients to name themselves rather than appear
  as a generic SDK, and
* a stable per-conversation ``x-opencode-session`` id, so requests route
  consistently and prompt caching can hit. ``OPENCODE_GO_SESSION_ID`` pins it
  across processes, which a scheduled job wants so separate runs share one
  conversation.

Both live in a ``chat_class`` subclass, the seam this codebase already uses for
provider quirks (MiniMax, DeepSeek, ChatGPT), so the shared client and its
``ProviderSpec`` stay untouched: the fork adds one registry row from here
instead of a field and a request-branch in `openai_client.py`.

The row is registered on import, and `llm_clients/__init__` imports this module
for that side effect. The base URL is the documented one; ``backend_url`` can
pin any other endpoint the gateway serves.
"""

from __future__ import annotations

import os
import uuid

from tradingagents import __version__

from .openai_client import OPENAI_COMPATIBLE_PROVIDERS, LocalCompatibleChatOpenAI, ProviderSpec

BASE_URL = "https://opencode.ai/inference/go/openai/v1"

_SESSION_ID: str | None = None


def session_id() -> str:
    """The OpenCode Go session id: ``OPENCODE_GO_SESSION_ID``, else one per process.

    Generated once and kept: a fresh id per request would defeat both the routing
    affinity and the prompt cache the header exists for.
    """
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = (os.environ.get("OPENCODE_GO_SESSION_ID") or uuid.uuid4().hex).strip()
    return _SESSION_ID


def _headers() -> dict[str, str]:
    """The headers OpenCode Go expects beyond the bearer token."""
    return {
        "User-Agent": f"TradingAgents/{__version__}",
        "x-opencode-session": session_id(),
    }


class OpenCodeGoChatOpenAI(LocalCompatibleChatOpenAI):
    """Local-compatible client that identifies itself and sends the session id."""

    def __init__(self, **kwargs):
        headers = dict(kwargs.pop("default_headers", None) or {})
        for name, value in _headers().items():
            headers.setdefault(name, value)
        super().__init__(default_headers=headers, **kwargs)


OPENAI_COMPATIBLE_PROVIDERS["opencode-go"] = ProviderSpec(
    base_url=BASE_URL,
    chat_class=OpenCodeGoChatOpenAI,
)
