"""Phase 1 Sub-Agent.

ReAct agent that discovers new Guard/Action vocabulary from a single client's
source code using hybrid search (Mode A).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Template

from config import LANGUAGE_GRAMMARS
from state import VocabDiscoveryReport, VocabEntry
from utils import invoke_with_retry

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase1_sub.j2"


def _load_prompt_template() -> Template:
    return Template(_PROMPT_PATH.read_text(encoding="utf-8"))


def build_phase1_sub_agent(client_name: str, llm=None, callbacks=None):
    """Build a Phase 1 Sub-Agent for *client_name*.

    If *llm* is None, returns a mock implementation.
    """
    lang_key, _ = LANGUAGE_GRAMMARS[client_name]

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        """Execute the sub-agent: discover new vocabulary."""
        guards = state.get("guards", [])
        actions = state.get("actions", [])
        vocab_version = state.get("vocab_version", 0)
        iteration = state.get("phase1_iteration", 1)

        template = _load_prompt_template()
        _prompt = template.render(
            client_name=client_name,
            language=lang_key,
            guards=guards,
            actions=actions,
            vocab_version=vocab_version,
        )

        if llm is not None:
            try:
                chain = llm.with_structured_output(VocabDiscoveryReport)
                report: VocabDiscoveryReport = invoke_with_retry(
                    chain, _prompt, label=f"phase1_sub/{client_name}",
                    callbacks=callbacks,
                )
                report_dict = report.model_dump()
                report_dict["iteration"] = iteration
                return {
                    "discovery_reports": [report_dict],
                }
            except Exception:
                logger.error("LLM call failed for %s", client_name, exc_info=True)

        # Mock fallback
        logger.info("[phase1_sub_agent] client=%s — using mock response", client_name)
        report_dict = {
            "client_name": client_name,
            "new_guards": [],
            "new_actions": [],
            "iteration": iteration,
        }
        return {"discovery_reports": [report_dict]}

    return _run
