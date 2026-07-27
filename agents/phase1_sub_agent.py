"""Phase 1 Sub-Agent.

Discovers new Guard/Action vocabulary from a single client's source code.

Design (I/O-efficient, evidence-grounded):
  1. PLAN   — a small LLM call turns the current vocabulary gaps into a list of
              code-search queries (tiny input/output, no code).
  2. SEARCH — the program runs those queries locally against the client's
              call-graph-directed index (tools.search). No LLM involved.
  3. EXTRACT— a second LLM call sees ONLY the curated, deduplicated snippets
              (with real file/function/line metadata) and extracts vocabulary,
              citing the snippet each entry came from.

Every discovered entry's evidence is validated against the real retrieved
snippet set; ungrounded entries (hallucinated file paths) are dropped. This
keeps token I/O bounded and prevents the "vocabulary with invented evidence"
problem that an un-grounded single LLM call produces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from config import LANGUAGE_GRAMMARS, WORKFLOW_IDS
from state import VocabDiscoveryReport

from agents._prompts import load_template
from agents._llm import invoke_structured
from agents._evidence import (
    norm_path as _norm_path,
    validate_vocab_entries as _validate_entries,
)

logger = logging.getLogger(__name__)

_PLAN_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase1_plan.j2"
_EXTRACT_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase1_sub.j2"

# Tunables — keep the snippet pool (and thus extraction-call input) bounded.
_TOP_K_PER_QUERY = 6
_MAX_QUERIES = 16
_MAX_SNIPPETS = 40
_SNIPPET_CHARS = 320

# Compact one-line workflow descriptions used only for query planning.
_WF_DESCRIPTIONS: dict[str, str] = {
    "initial_sync": "bulk-download blocks from peers via range requests until synced",
    "regular_sync": "maintain head via gossip: validate/import blocks & attestations, handle reorgs",
    "checkpoint_sync": "bootstrap from a trusted finalized checkpoint, verify anchor, backfill",
    "attestation_generate": "validator builds, slashing-checks, signs and publishes an attestation",
    "block_generate": "validator builds an execution+beacon block via Engine API and broadcasts it",
    "aggregate": "aggregator collects attestations into an AggregateAndProof and publishes it",
    "execute_layer_relation": "CL<->EL Engine API: newPayload/forkchoiceUpdated, optimistic & invalid handling",
}


class SearchQuery(BaseModel):
    """A single planned code-search query targeting a workflow."""
    workflow_id: str = ""
    query: str = ""


class SearchPlan(BaseModel):
    """Output of the query-planning LLM call."""
    queries: list[SearchQuery] = Field(default_factory=list)


def _load_template(path: Path):
    return load_template(path)


def _plan_queries(
    llm: Any,
    *,
    client_name: str,
    language: str,
    guard_names: list[str],
    action_names: list[str],
    callbacks: Any,
) -> list[SearchQuery]:
    """PLAN step: ask the LLM for compact search queries (no code in/out)."""
    template = _load_template(_PLAN_PROMPT_PATH)
    workflows = [
        {"id": wf, "desc": _WF_DESCRIPTIONS.get(wf, wf)} for wf in WORKFLOW_IDS
    ]
    prompt = template.render(
        client_name=client_name,
        language=language,
        guard_names=guard_names,
        action_names=action_names,
        workflows=workflows,
        max_queries=_MAX_QUERIES,
    )
    # R4: let exceptions propagate so the caller can mark this client as failed
    # (previously swallowed into [] and indistinguishable from "no queries").
    plan = invoke_structured(
        llm, SearchPlan, prompt,
        label=f"phase1_plan/{client_name}", callbacks=callbacks,
    )
    queries = [q for q in plan.queries if q.query.strip()]
    return queries[:_MAX_QUERIES]


def _retrieve_snippets(
    queries: list[SearchQuery], client_name: str,
) -> list[dict[str, Any]]:
    """SEARCH step: run the planned queries locally; no LLM involved."""
    try:
        from tools.search import search_codebase, search_codebase_by_workflow
    except ImportError:
        logger.warning("tools.search unavailable — skipping retrieval for %s", client_name)
        return []

    valid_wfs = set(WORKFLOW_IDS)
    snippets: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    for q in queries:
        if len(snippets) >= _MAX_SNIPPETS:
            break
        wf = q.workflow_id if q.workflow_id in valid_wfs else ""
        try:
            if wf:
                results = search_codebase_by_workflow(
                    workflow_id=wf, query=q.query,
                    client_name=client_name, top_k=_TOP_K_PER_QUERY,
                )
            else:
                results = search_codebase(q.query, client_name, top_k=_TOP_K_PER_QUERY)
        except Exception:
            logger.debug("search failed q=%s", q.query, exc_info=True)
            continue

        for r in results:
            file_path = r.metadata.get("file_path", "")
            lines = [r.metadata.get("start_line", 0), r.metadata.get("end_line", 0)]
            key = (file_path, lines[0], lines[1])
            if key in seen:
                continue
            seen.add(key)
            snippets.append({
                "id": f"{client_name}::{len(snippets) + 1}",
                "workflow_id": wf or "general",
                "file": file_path,
                "function": r.metadata.get("function_name", ""),
                "lines": lines,
                "snippet": (r.content or "")[:_SNIPPET_CHARS],
            })
            if len(snippets) >= _MAX_SNIPPETS:
                break
    return snippets


# _norm_path and _validate_entries now live in agents._evidence (imported
# above as _norm_path / _validate_entries) — kept as the public names.


def build_phase1_sub_agent(client_name: str, llm=None, callbacks=None):
    """Build a Phase 1 Sub-Agent for *client_name*.

    If *llm* is None, returns a mock implementation (empty discovery).
    """
    lang_key, _ = LANGUAGE_GRAMMARS[client_name]

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        guards = state.get("guards", [])
        actions = state.get("actions", [])
        iteration = state.get("phase1_iteration", 1)

        def _empty(failed: bool = False) -> dict[str, Any]:
            return {"discovery_reports": [{
                "client_name": client_name,
                "new_guards": [],
                "new_actions": [],
                "iteration": iteration,
            }],
            # R4: surface LLM failures so the phase1 router doesn't mistake a
            # total failure (e.g. out-of-credit 429s) for convergence.
            "phase1_sub_errors": 1 if failed else 0,
            "phase1_sub_failed_clients": [client_name] if failed else []}

        if llm is None:
            logger.info("[phase1_sub_agent] client=%s — mock (no llm)", client_name)
            return _empty()

        guard_names = [g.get("name", "") for g in guards if g.get("name")]
        action_names = [a.get("name", "") for a in actions if a.get("name")]

        # 1) PLAN — compact queries.
        try:
            queries = _plan_queries(
                llm, client_name=client_name, language=lang_key,
                guard_names=guard_names, action_names=action_names, callbacks=callbacks,
            )
        except Exception:
            logger.error("Query planning failed for %s", client_name, exc_info=True)
            return _empty(failed=True)
        if not queries:
            logger.info("[phase1_sub_agent] client=%s — no queries planned", client_name)
            return _empty()

        # 2) SEARCH — local retrieval, no LLM.
        snippets = _retrieve_snippets(queries, client_name)
        logger.info(
            "[phase1_sub_agent] client=%s queries=%d snippets=%d",
            client_name, len(queries), len(snippets),
        )
        if not snippets:
            return _empty()

        # 3) EXTRACT — LLM sees only curated snippets; evidence is validated.
        template = _load_template(_EXTRACT_PROMPT_PATH)
        prompt = template.render(
            client_name=client_name,
            language=lang_key,
            guard_names=guard_names,
            action_names=action_names,
            snippets=snippets,
        )
        try:
            report = invoke_structured(
                llm, VocabDiscoveryReport, prompt,
                label=f"phase1_sub/{client_name}", callbacks=callbacks,
            )
            report_dict = report.model_dump()
        except Exception:
            logger.error("Extraction failed for %s", client_name, exc_info=True)
            return _empty(failed=True)

        allowed_files = {_norm_path(s["file"]) for s in snippets if s["file"]}
        allowed_basenames = {f.rsplit("/", 1)[-1] for f in allowed_files}

        before_g = len(report_dict.get("new_guards", []))
        before_a = len(report_dict.get("new_actions", []))
        report_dict["new_guards"] = _validate_entries(
            report_dict.get("new_guards", []), allowed_files, allowed_basenames,
        )
        report_dict["new_actions"] = _validate_entries(
            report_dict.get("new_actions", []), allowed_files, allowed_basenames,
        )
        report_dict["client_name"] = client_name
        report_dict["iteration"] = iteration

        dropped = (before_g - len(report_dict["new_guards"])) + (
            before_a - len(report_dict["new_actions"]))
        if dropped:
            logger.info(
                "[phase1_sub_agent] client=%s dropped %d ungrounded entries",
                client_name, dropped,
            )
        return {"discovery_reports": [report_dict]}

    return _run
