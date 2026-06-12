"""Phase 2.5 — Scenario Scan Agent.

Runs after a workflow's Phase 2 converges.  For each fault scenario relevant
to the workflow, it:

1. Retrieves scenario-specific code via targeted RAG search (no LLM).
2. Asks the LLM: "Is this scenario represented in the current LSG?  If yes,
   tag the relevant transitions; if no, propose what to add."
3. Returns ScenarioCoverage objects (one per client × scenario pair).

The caller (graph.py) uses the coverage results to decide whether to re-enter
Phase 2 with scenario hints (at most once per workflow).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Template
from pydantic import BaseModel, Field

from config import (
    CLIENT_NAMES,
    SCENARIO_SCAN_MAX_SNIPPETS,
    SCENARIO_SCAN_TOP_K,
    SCENARIOS,
    Scenario,
)
from state import ScenarioCoverage
from utils import invoke_with_retry

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase2_scenario.j2"


def _load_template() -> Template:
    return Template(_PROMPT_PATH.read_text(encoding="utf-8"))


# ── Retrieval (program, no LLM) ──────────────────────────────────────────────


def _retrieve_scenario_snippets(
    client_name: str,
    workflow_id: str,
    scenario: Scenario,
) -> list[dict]:
    """Search for code relevant to *scenario* in *client_name*'s index."""
    try:
        from tools.search import search_codebase_by_workflow
    except ImportError:
        return []

    snippets: list[dict] = []
    seen: set[tuple] = set()

    for query in scenario.search_queries:
        if len(snippets) >= SCENARIO_SCAN_MAX_SNIPPETS:
            break
        try:
            results = search_codebase_by_workflow(
                workflow_id=workflow_id,
                query=query,
                client_name=client_name,
                top_k=SCENARIO_SCAN_TOP_K,
            )
            for r in results:
                fp = r.metadata.get("file_path", "")
                sl = r.metadata.get("start_line", 0)
                if (fp, sl) in seen:
                    continue
                seen.add((fp, sl))
                snippets.append({
                    "id": f"{client_name}::{scenario.id}::S{len(snippets)+1}",
                    "file": fp,
                    "function": r.metadata.get("function_name", ""),
                    "start_line": sl,
                    "end_line": r.metadata.get("end_line", 0),
                    "snippet": (r.content or "")[:400],
                })
        except Exception:
            logger.debug("scenario search failed q=%s", query, exc_info=True)

    return snippets[:SCENARIO_SCAN_MAX_SNIPPETS]


# ── LLM output schema ─────────────────────────────────────────────────────────


class _ScenarioCoverageResult(BaseModel):
    client_name: str
    scenario_id: str
    covered: bool = False
    state_ids: list[str] = Field(default_factory=list)
    transition_guards: list[str] = Field(default_factory=list)
    evidence_file: str = ""
    evidence_function: str = ""
    evidence_lines: list[int] = Field(default_factory=list)
    notes: str = ""
    # If covered=False, LLM proposes state/transition(s) to add in next iter
    suggested_transitions: list[dict] = Field(default_factory=list)


def _serialize_wf(lsg: dict, wf_id: str) -> str:
    """Return a compact text representation of the workflow's states/transitions."""
    import yaml
    for wf in lsg.get("workflows", []):
        if wf.get("id") == wf_id:
            lines = [f"workflow: {wf_id}"]
            for st in wf.get("states", []):
                lines.append(f"  state: {st['id']} [{st.get('category','')}]")
                for tr in st.get("transitions", []):
                    lines.append(f"    → {tr['guard']} → {tr['next_state']}")
            return "\n".join(lines)
    return f"(workflow {wf_id} not found in LSG)"


# ── Per-client scenario evaluation ───────────────────────────────────────────


def _evaluate_one(
    client_name: str,
    workflow_id: str,
    lsg: dict,
    scenario: Scenario,
    llm: Any,
    callbacks: Any,
) -> ScenarioCoverage:
    """Run one client × one scenario evaluation."""
    snippets = _retrieve_scenario_snippets(client_name, workflow_id, scenario)
    wf_text = _serialize_wf(lsg, workflow_id)

    valid_files = {s["file"] for s in snippets if s["file"]}
    valid_basenames = {f.rsplit("/", 1)[-1] for f in valid_files}

    template = _load_template()
    prompt = template.render(
        client_name=client_name,
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        scenario_trigger=scenario.trigger,
        scenario_risk=scenario.risk,
        workflow_id=workflow_id,
        workflow_lsg=wf_text,
        snippets=snippets,
    )

    try:
        chain = llm.with_structured_output(_ScenarioCoverageResult)
        result: _ScenarioCoverageResult = invoke_with_retry(
            chain, prompt,
            label=f"phase2_scenario/{client_name}/{scenario.id}",
            callbacks=callbacks,
        )
        # Ground evidence file against retrieved snippets
        ev_file = result.evidence_file.replace("\\", "/").lower().strip()
        ev_base = ev_file.rsplit("/", 1)[-1] if ev_file else ""
        ev = None
        if ev_file and (ev_file in {f.lower() for f in valid_files}
                        or ev_base in {f.lower() for f in valid_basenames}):
            ev = {
                "file": result.evidence_file,
                "function": result.evidence_function,
                "lines": result.evidence_lines,
            }

        return ScenarioCoverage(
            client=client_name,
            scenario_id=scenario.id,
            workflow_id=workflow_id,
            covered=result.covered,
            state_ids=result.state_ids,
            transition_guards=result.transition_guards,
            evidence=ev,
            notes=result.notes,
            suggested_transitions=result.suggested_transitions,
        )
    except Exception:
        logger.error(
            "Scenario scan LLM failed %s/%s/%s",
            client_name, workflow_id, scenario.id,
            exc_info=True,
        )
        return ScenarioCoverage(
            client=client_name,
            scenario_id=scenario.id,
            workflow_id=workflow_id,
        )


# ── Public builder ─────────────────────────────────────────────────────────────


def build_phase2_scenario_agent(client_name: str, llm: Any = None, callbacks: Any = None):
    """Build a Phase 2.5 Scenario Scan Agent for *client_name*.

    Evaluates all scenarios relevant to the current workflow.
    """

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        current_wf = state.get("current_workflow", "")
        existing_lsg = state.get("client_lsgs", {}).get(client_name, {})

        relevant = [s for s in SCENARIOS if current_wf in s.relevant_workflows]
        if not relevant:
            return {}

        if llm is None:
            # Mock: return uncovered for all scenarios
            coverages: dict[str, dict] = {}
            for sc in relevant:
                key = f"{client_name}::{current_wf}::{sc.id}"
                coverages[key] = ScenarioCoverage(
                    client=client_name,
                    scenario_id=sc.id,
                    workflow_id=current_wf,
                ).model_dump()
            return {"scenario_coverages": coverages}

        coverages = {}
        for sc in relevant:
            cov = _evaluate_one(
                client_name, current_wf, existing_lsg, sc, llm, callbacks,
            )
            key = f"{client_name}::{current_wf}::{sc.id}"
            coverages[key] = cov.model_dump()
            logger.info(
                "[phase2_scenario] client=%s wf=%s scenario=%s covered=%s",
                client_name, current_wf, sc.id, cov.covered,
            )

            # If covered, annotate the LSG transitions with scenario_id
            if cov.covered and cov.transition_guards:
                _annotate_lsg(existing_lsg, current_wf, cov.transition_guards, sc.id)

        return {
            "scenario_coverages": coverages,
            "client_lsgs": {client_name: existing_lsg},
        }

    return _run


def _annotate_lsg(lsg: dict, wf_id: str, guards: list[str], scenario_id: str) -> None:
    """Add *scenario_id* to matching transitions in the LSG (in-place)."""
    for wf in lsg.get("workflows", []):
        if wf.get("id") != wf_id:
            continue
        for st in wf.get("states", []):
            for tr in st.get("transitions", []):
                if tr.get("guard") in guards:
                    existing = tr.get("scenario_ids", [])
                    if scenario_id not in existing:
                        tr["scenario_ids"] = existing + [scenario_id]


def get_scenario_hints_for_reiter(
    state: dict[str, Any],
    workflow_id: str,
) -> list[dict]:
    """Extract scenario gaps as Phase 2 sparsity-style hints.

    Called by the graph router. Returns hint dicts for scenarios that are
    uncovered in ≥ 1 client AND haven't already triggered a re-iteration.
    """
    coverages = state.get("scenario_coverages", {})
    triggered = set(state.get("scenario_triggered_reiter", []))

    hints: list[dict] = []
    for key, cov in coverages.items():
        if not key.endswith(f"::{workflow_id}::") and workflow_id not in key:
            continue
        if cov.get("workflow_id") != workflow_id:
            continue
        if cov.get("covered"):
            continue
        reiter_key = f"{workflow_id}::{cov['scenario_id']}"
        if reiter_key in triggered:
            continue
        # Build hint
        suggested = cov.get("suggested_transitions", [])
        hint_text = (
            f"Scenario '{cov['scenario_id']}' ({cov.get('notes', 'no notes')}): "
            "not found in LSG. "
        )
        if suggested:
            hint_text += (
                f"Suggested: add state/transition for "
                f"{', '.join(t.get('guard','?') for t in suggested[:2])}."
            )
        hints.append({
            "workflow_id": workflow_id,
            "client": cov.get("client", ""),
            "scenario_id": cov["scenario_id"],
            "states": 0,
            "transitions": 0,
            "hint": hint_text,
        })
    return hints
