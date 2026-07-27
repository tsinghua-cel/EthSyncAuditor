"""Parameter / behavior-divergence Sub-Agent (subsystem domains).

Per-client × subsystem-domain extraction of parameters/behaviors (constants,
thresholds, algorithm shapes) from retrieved code. Mirrors phase1_sub's
plan→search→extract, evidence-grounded shape, but emits ParameterValues keyed
by aspect id instead of guard/action vocabulary.

The cross-client grouping that turns these into ParameterDivergence happens in
``param_main_agent.compare_domain`` (deterministic, no LLM).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from config import LANGUAGE_GRAMMARS, get_domain
from state import BehaviorAspect, Evidence, ParameterValue

from agents._prompts import load_template
from agents._llm import invoke_structured
from agents._evidence import build_evidence_pool, is_real_evidence
from agents._retrieval import SnippetAccumulator

logger = logging.getLogger(__name__)


class _ParamAspect(BaseModel):
    """One extracted parameter/behavior for one client."""

    id: str                            # short stable slug, e.g. "udp_rate_limit"
    name: str
    aspect_type: str = "parameter"     # parameter | algorithm_shape | constant | presence
    unit: str = ""
    value: str
    value_type: str = "literal"        # literal | absent | defined_but_unused | structural
    evidence_file: str = ""
    evidence_function: str = ""
    evidence_lines: list[int] = Field(default_factory=list)
    notes: str = ""


class _ParamExtractionReport(BaseModel):
    client_name: str
    aspects: list[_ParamAspect] = Field(default_factory=list)


def _retrieve(domain, client_name: str) -> list[dict]:
    """Retrieve subsystem code via the domain's seeded queries (whole-index
    hybrid search — subsystems are not workflow-scoped)."""
    try:
        from tools.search import search_codebase_by_domain
    except ImportError:
        logger.debug("[param_sub] tools.search unavailable")
        return []

    acc = SnippetAccumulator(
        id_prefix=f"{client_name}::{domain.id}",
        max_snippets=domain.max_snippets,
        max_total_chars=domain.max_total_chars,
        snippet_chars=domain.snippet_chars,
    )
    for q in domain.retrieval_queries:
        if acc.full:
            break
        try:
            for r in search_codebase_by_domain(
                domain.id, q, client_name,
                max_call_depth=domain.max_call_depth, top_k=domain.top_k_per_query,
            ):
                acc.add(r)
        except Exception:
            logger.debug("[param_sub] query failed q=%s", q, exc_info=True)
    logger.info("[param_sub] client=%s domain=%s snippets=%d",
                client_name, domain.id, len(acc.snippets))
    return acc.result()


def build_param_sub_agent(client_name: str, llm: Any = None, callbacks: Any = None):
    """Build a parameter-track sub-agent for *client_name*.

    If *llm* is None (mock), returns no aspects.
    """
    lang_key, _ = LANGUAGE_GRAMMARS[client_name]

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        domain_id = state.get("current_domain", "")
        domain = get_domain(domain_id)
        if domain is None:
            return {}
        if llm is None:
            return {}

        snippets = _retrieve(domain, client_name)
        if not snippets:
            return {}

        template = load_template("param_sub.j2")
        prompt = template.render(
            client_name=client_name,
            language=lang_key,
            domain=domain,
            snippets=snippets,
        )
        try:
            report = invoke_structured(
                llm, _ParamExtractionReport, prompt,
                label=f"param_sub/{client_name}/{domain_id}",
                callbacks=callbacks,
            )
        except Exception:
            logger.error("[param_sub] extraction failed %s/%s", client_name, domain_id, exc_info=True)
            return {}

        pool = build_evidence_pool(snippets, client_name)
        client_map: dict[str, dict] = {}
        aspect_map: dict[str, dict] = {}
        for asp in report.aspects:
            slug = asp.id.strip()
            if not slug:
                continue
            aid = f"{domain_id}::{slug}"
            ev: Evidence | None = None
            if asp.evidence_file:
                evd = {
                    "file": asp.evidence_file,
                    "function": asp.evidence_function,
                    "lines": asp.evidence_lines,
                }
                if is_real_evidence(evd, pool, client_name, strict=False):
                    ev = Evidence(**evd)  # type: ignore[arg-type]
            pv = ParameterValue(
                client=client_name, value=asp.value, value_type=asp.value_type,
                evidence=ev, notes=asp.notes,
            )
            client_map[aid] = pv.model_dump()
            aspect_map[aid] = BehaviorAspect(
                id=aid, domain_id=domain_id, name=asp.name,
                aspect_type=asp.aspect_type, unit=asp.unit,
            ).model_dump()

        logger.info("[param_sub] client=%s domain=%s aspects=%d",
                    client_name, domain_id, len(client_map))
        return {
            "client_aspects": {client_name: client_map},
            "behavior_aspects": aspect_map,
        }

    return _run
