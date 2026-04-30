"""Phase 1 Main Agent.

Merges vocabulary discovery reports from sub-agents, deduplicates, computes
diff_rate, and outputs the enriched specification.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Template

from state import EnrichedSpec, VocabEntry
from utils import invoke_with_retry

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase1_main.j2"


def _load_prompt_template() -> Template:
    return Template(_PROMPT_PATH.read_text(encoding="utf-8"))


def build_phase1_main_agent(llm=None, callbacks=None):
    """Build the Phase 1 Main Agent.

    If *llm* is None, returns a deterministic merge implementation.
    """

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        """Merge discovery reports, deduplicate, compute diff_rate."""
        reports = state.get("discovery_reports", [])
        existing_guards = list(state.get("guards", []))
        existing_actions = list(state.get("actions", []))
        vocab_version = state.get("vocab_version", 0)

        if llm is not None:
            template = _load_prompt_template()
            _prompt = template.render(
                guards=existing_guards,
                actions=existing_actions,
                vocab_version=vocab_version,
                discovery_reports=reports,
            )
            try:
                chain = llm.with_structured_output(EnrichedSpec)
                spec: EnrichedSpec = invoke_with_retry(
                    chain, _prompt, label="phase1_main",
                    callbacks=callbacks,
                )
                new_guards = [g.model_dump() for g in spec.guards
                              if g.name not in {eg["name"] for eg in existing_guards}]
                new_actions = [a.model_dump() for a in spec.actions
                               if a.name not in {ea["name"] for ea in existing_actions}]
                total = len(existing_guards) + len(existing_actions) + len(new_guards) + len(new_actions)
                diff_rate = (len(new_guards) + len(new_actions)) / max(total, 1)

                return {
                    "guards": new_guards,
                    "actions": new_actions,
                    "vocab_version": vocab_version + 1,
                    "diff_rate": diff_rate,
                    "discovery_reports": [],
                }
            except Exception:
                logger.error("LLM call failed for phase1_main", exc_info=True)

        # Deterministic merge fallback
        logger.info("[phase1_main_agent] deterministic merge of %d reports", len(reports))
        new_guards: list[dict] = []
        new_actions: list[dict] = []
        seen_guard_names = {g["name"] for g in existing_guards}
        seen_action_names = {a["name"] for a in existing_actions}

        for report in reports:
            for g in report.get("new_guards", []):
                if g["name"] not in seen_guard_names:
                    new_guards.append(g)
                    seen_guard_names.add(g["name"])
            for a in report.get("new_actions", []):
                if a["name"] not in seen_action_names:
                    new_actions.append(a)
                    seen_action_names.add(a["name"])

        total = len(existing_guards) + len(existing_actions) + len(new_guards) + len(new_actions)
        diff_rate = (len(new_guards) + len(new_actions)) / max(total, 1)

        return {
            "guards": new_guards,
            "actions": new_actions,
            "vocab_version": vocab_version + 1,
            "diff_rate": diff_rate,
            "discovery_reports": [],
        }

    return _run
