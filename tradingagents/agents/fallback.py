"""What happens when a structured-output agent falls back to free text.

Kept out of ``structured.py`` (an upstream file) so the fork's addition is one
import and two calls there, not a rewritten module. The rule it implements is
the investor's P1-6: a fallback is a degraded artifact, so it must be visible
at the top of the report, recorded with the parser's reason for the run log,
checked for the labels downstream readers need, and able to fail ``--strict``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# A provider that cannot return the schema still has to produce a decision, so
# the agent falls back to free text. That is a degraded artifact, not a normal
# result: the marker heads the report and strict mode turns it into an exit
# code, so a reader cannot mistake an unvalidated fallback for a parsed one.
FREETEXT_FALLBACK_MARKER = (
    "> ⚠️ Fallback: generated as free text; structured-output validation did not run."
)

# Only the agents whose output is a decision carry the fallback marker and the
# strict-mode failure. A sentiment report that degrades to prose is a style
# change, not a decision nobody can parse.
DECISION_AGENTS = frozenset({"Portfolio Manager", "Trader", "Research Manager"})

# What each structured agent's fallback text must still carry, so a fallback
# that lost the decision itself is distinguished from one that only lost the
# schema. These are the labels the rest of the pipeline reads (the rating line,
# the transaction line, the plan recommendation).
REQUIRED_FALLBACK_LABELS = {
    "Portfolio Manager": (re.compile(r"\*\*Rating\*\*\s*:"),),
    "Trader": (re.compile(r"FINAL TRANSACTION PROPOSAL\s*:"),),
    "Research Manager": (re.compile(r"\*\*Recommendation\*\*\s*:"),),
}

# One entry per structured-output failure in this process: (agent, raw reason,
# labels the fallback text was missing). The CLI writes these to the run log.
_FALLBACK_EVENTS: list[tuple[str, str, tuple[str, ...]]] = []
# The parser's reason for the latest failure of each agent, consumed when the
# fallback text is assembled (so the event is recorded once, with its labels).
_LAST_REASON: dict[str, str] = {}


def fallback_events() -> list[tuple[str, str, tuple[str, ...]]]:
    """The structured-output failures recorded so far, for the run log and strict mode."""
    return list(_FALLBACK_EVENTS)


def reset_fallback_events() -> None:
    """Forget the recorded failures (a new run in the same process starts clean)."""
    _FALLBACK_EVENTS.clear()
    _LAST_REASON.clear()


def last_reason(agent_name: str) -> str:
    """The parser reason recorded for an agent's latest structured failure."""
    return _LAST_REASON.get(agent_name, "")


def record_structured_failure(agent_name: str, exc: Exception) -> str:
    """Log a structured-output failure at WARNING and remember its truncated reason.

    The raw parser error is what a post-mortem needs; the first 200 characters
    are enough to identify it and short enough not to flood the log.
    """
    reason = str(exc)[:200]
    _LAST_REASON[agent_name] = reason
    logger.warning(
        "%s: structured-output invocation failed (%s); retrying once as free text. "
        "Raw parser error (first 200 chars): %s",
        agent_name, exc, reason,
    )
    return reason


def missing_fallback_labels(agent_name: str, text: str) -> tuple[str, ...]:
    """Required label patterns absent from a free-text fallback."""
    return tuple(
        pattern.pattern for pattern in REQUIRED_FALLBACK_LABELS.get(agent_name, ())
        if not pattern.search(text or "")
    )


def free_text_with_warning(agent_name: str, text: str, reason: str = "") -> str:
    """Head a decision fallback with the marker, and say when its labels are missing.

    The marker goes first so a reader sees the degradation before the decision.
    A missing required label is appended as a second warning and recorded, since
    a fallback that dropped the decision text is worse than one that kept it.
    A non-decision agent (the sentiment analyst) keeps its prose unchanged: the
    spec's degraded artifact is a decision, and marking every report would make
    every run look unclean.
    """
    if agent_name not in DECISION_AGENTS:
        return text or ""
    if FREETEXT_FALLBACK_MARKER in (text or ""):
        return text
    missing = missing_fallback_labels(agent_name, text)
    _FALLBACK_EVENTS.append((agent_name, reason[:200], missing))
    body = (text or "").lstrip()
    if missing:
        escaped = ", ".join(missing)
        return (
            f"{FREETEXT_FALLBACK_MARKER}\n"
            f"> ⚠️ Fallback validation: required decision label(s) missing: {escaped}.\n\n"
            f"{body}"
        )
    return f"{FREETEXT_FALLBACK_MARKER}\n\n{body}"


def missing_decision_labels(final_state: dict) -> list[str]:
    """The labels a finished run's decision artifacts must carry, and lack.

    ``**Rating**:`` identifies the Portfolio Manager's call and
    ``FINAL TRANSACTION PROPOSAL:`` the Trader's; without them the run produced
    prose no downstream reader can parse.
    """
    missing = []
    decision = final_state.get("final_trade_decision") or ""
    trader = final_state.get("trader_investment_plan") or ""
    for agent, text, label in (
        ("Portfolio Manager", decision, "**Rating**: (final decision)"),
        ("Trader", trader, "FINAL TRANSACTION PROPOSAL: (trader plan)"),
    ):
        if not any(pattern.search(text) for pattern in REQUIRED_FALLBACK_LABELS[agent]):
            missing.append(label)
    return missing
