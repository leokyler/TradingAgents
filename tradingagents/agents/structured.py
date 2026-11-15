"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires. What a fallback
means for the report, the run log and ``--strict`` lives in
``tradingagents.agents.fallback`` (a fork module), so this upstream file only
calls into it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from tradingagents.agents.fallback import (
    FREETEXT_FALLBACK_MARKER,
    fallback_events,
    free_text_with_warning,
    last_reason,
    missing_decision_labels,
    record_structured_failure,
    reset_fallback_events,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FREETEXT_FALLBACK_MARKER",
    "NO_EXTERNAL_TOOLS",
    "bind_structured",
    "fallback_events",
    "free_text_with_warning",
    "invoke_structured",
    "invoke_structured_or_freetext",
    "last_reason",
    "missing_decision_labels",
    "reset_fallback_events",
]

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.

    A failure is logged at WARNING with the parser's own reason (the first 200
    characters, so a giant provider payload does not flood the log), and the
    failure is recorded for the run log and strict mode. The caller must then
    use :func:`free_text_with_warning`, not the bare free-text answer.
    """
    if structured_llm is None:
        return None
    try:
        result = structured_llm.invoke(prompt)
        if result is None:
            # A thinking model can answer in plain text instead of calling
            # the tool, leaving the parser with nothing to return. Treat it
            # as a structured miss and fall back, with a clear reason.
            raise ValueError("structured output returned no parsed result")
        return result
    except Exception as exc:
        record_structured_failure(agent_name, exc)
        return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    The fallback is headed by :data:`FREETEXT_FALLBACK_MARKER` so the degraded
    artifact is identifiable in the report, the memory log and strict mode.
    """
    # No structured binding means the provider cannot return the schema at all;
    # free text is then the intended path, not a degraded one, so no fallback is
    # recorded and strict mode does not fail an otherwise normal run.
    if structured_llm is None:
        return plain_llm.invoke(prompt).content
    result = invoke_structured(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    text = plain_llm.invoke(prompt).content
    return free_text_with_warning(agent_name, text, last_reason(agent_name))
