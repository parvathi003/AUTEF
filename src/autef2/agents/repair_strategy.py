"""Repair Strategy Agent: which repair matches this cause?

The mapping is deterministic by default -- a root cause selects its ladder, and
the next unattempted rung is the strategy. Determinism matters here: it makes
the escalation path reproducible, it costs nothing, and it guarantees the
framework never tries the same approach twice, which was the specific gap in
v1 (one prompt, one attempt, no alternative).

The model is consulted in exactly one situation: the ladder for the diagnosed
cause is exhausted, attempts remain, and the failure is still there. At that
point the original diagnosis has been contradicted by evidence -- two matched
repairs did not work -- so the useful question is no longer "next rung" but
"which other cause's repair should we borrow". That is a judgement call, and it
is the one place a model adds something the table cannot.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from ..config import AutefConfig
from ..llm import LLMClient, LLMError
from ..models import Diagnosis, RootCause, Strategy, TestFailure
from ..strategies import LADDERS, ladder_for, strategy_by_id

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """
You are choosing a repair strategy for a failing Python test.

The repairs matched to the originally diagnosed cause have all been applied and
re-run, and the test still fails. So the original diagnosis is probably wrong.
Pick the strategy from the list below that is most likely to work given what
the failure looks like NOW.

Do not pick a strategy that has already been tried. Reply with a single JSON
object: {"strategy_id": "...", "reason": "one sentence"}
""".strip()


class RepairStrategyAgent:
    """Chooses the next repair strategy, escalating rather than repeating."""

    def __init__(self, llm: Optional[LLMClient], config: AutefConfig):
        self.llm = llm
        self.config = config

    def select(
        self,
        diagnosis: Diagnosis,
        failure: TestFailure,
        attempted: Sequence[str],
    ) -> Optional[Strategy]:
        """Next strategy to try, or None when nothing sensible is left."""
        attempted_set = set(attempted)

        for strategy in ladder_for(diagnosis.root_cause):
            if strategy.id not in attempted_set:
                logger.debug(
                    "Strategy for %s: %s (rung %d)",
                    failure.nodeid, strategy.id, strategy.rung,
                )
                return strategy

        return self._cross_ladder(diagnosis, failure, attempted_set)

    # -- fallback ---------------------------------------------------------

    def _cross_ladder(
        self,
        diagnosis: Diagnosis,
        failure: TestFailure,
        attempted: set,
    ) -> Optional[Strategy]:
        candidates = [
            strategy
            for cause, ladder in LADDERS.items()
            if cause != diagnosis.root_cause
            for strategy in ladder
            if strategy.id not in attempted and strategy.rung == 1
        ]
        if not candidates:
            return None

        if self.llm is None:
            return self._static_fallback(diagnosis, candidates)

        listing = "\n".join(f"- {s.id}: {s.label}" for s in candidates)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Test: {failure.nodeid}\n"
                    f"Original diagnosis: {diagnosis.root_cause.value} "
                    f"(confidence {diagnosis.confidence:.2f})\n"
                    f"Current exception: {failure.exception_type}: "
                    f"{failure.exception_message[:400]}\n\n"
                    f"Already tried: {sorted(attempted)}\n\n"
                    f"Available strategies:\n{listing}"
                ),
            },
        ]
        try:
            data = self.llm.complete_json(messages, required_keys=("strategy_id",))
        except LLMError as exc:
            logger.warning("Cross-ladder selection failed (%s)", exc)
            return self._static_fallback(diagnosis, candidates)

        chosen = strategy_by_id(str(data.get("strategy_id", "")).strip())
        if chosen is None or chosen.id in attempted:
            return self._static_fallback(diagnosis, candidates)

        logger.info(
            "Cross-ladder escalation for %s: %s (%s)",
            failure.nodeid, chosen.id, data.get("reason", ""),
        )
        return chosen

    def _static_fallback(
        self, diagnosis: Diagnosis, candidates: List[Strategy]
    ) -> Optional[Strategy]:
        """Without a model, prefer the neighbouring cause most often confused
        with the diagnosed one."""
        preference = _NEIGHBOURS.get(diagnosis.root_cause, [])
        by_cause = {
            cause: [s for s in ladder if s in candidates]
            for cause, ladder in LADDERS.items()
        }
        for cause in preference:
            options = by_cause.get(cause) or []
            if options:
                return options[0]
        return candidates[0] if candidates else None


#: Causes that present similarly enough to be worth trying when the first
#: diagnosis turns out to be wrong.
_NEIGHBOURS = {
    RootCause.ASSERTION_MISMATCH: [
        RootCause.API_MISUSE,
        RootCause.MOCK_MISCONFIGURATION,
        RootCause.FIXTURE_SETUP_ERROR,
    ],
    RootCause.API_MISUSE: [
        RootCause.ASSERTION_MISMATCH,
        RootCause.MOCK_MISCONFIGURATION,
        RootCause.IMPORT_ERROR,
    ],
    RootCause.MOCK_MISCONFIGURATION: [
        RootCause.API_MISUSE,
        RootCause.ASSERTION_MISMATCH,
        RootCause.FIXTURE_SETUP_ERROR,
    ],
    RootCause.IMPORT_ERROR: [
        RootCause.COLLECTION_ERROR,
        RootCause.API_MISUSE,
    ],
    RootCause.FIXTURE_SETUP_ERROR: [
        RootCause.API_MISUSE,
        RootCause.MOCK_MISCONFIGURATION,
    ],
    RootCause.COLLECTION_ERROR: [
        RootCause.IMPORT_ERROR,
    ],
    RootCause.UNKNOWN: [
        RootCause.ASSERTION_MISMATCH,
        RootCause.API_MISUSE,
        RootCause.MOCK_MISCONFIGURATION,
    ],
}
