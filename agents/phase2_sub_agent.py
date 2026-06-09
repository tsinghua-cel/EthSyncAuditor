"""Phase 2 Sub-Agent.

Extracts a **single workflow** LSG for one client using call-graph directed
hybrid search (Mode B).  The workflow to extract is specified by
``state["current_workflow"]``.

Design (evidence-grounded retrieval):
  - Multiple targeted queries (deterministic, per-workflow) replace the old
    single generic query, surfacing the actual processing functions instead of
    config/utility code.
  - Every retrieved snippet gets a stable evidence id (client::wf::Sn).
  - The extraction prompt shows ONLY these snippets and instructs the LLM to
    cite only files present in them.
  - Post-extraction evidence grounding: transitions whose evidence.file is not
    in the real retrieved pool are set to None rather than published as fact.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from config import CODE_BASE_PATH, LANGUAGE_GRAMMARS
from state import LSGFile
from utils import invoke_with_retry, summarize_vocab_for_prompt

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase2_sub.j2"


def _load_prompt_template() -> Template:
    return Template(_PROMPT_PATH.read_text(encoding="utf-8"))


def _extract_workflow(lsg: dict, wf_id: str) -> dict | None:
    for wf in lsg.get("workflows", []):
        if wf.get("id") == wf_id:
            return wf
    return None


def _replace_workflow(lsg: dict, wf_id: str, new_wf: dict) -> dict:
    updated = dict(lsg)
    new_workflows = []
    replaced = False
    for wf in lsg.get("workflows", []):
        if wf.get("id") == wf_id:
            new_workflows.append(new_wf)
            replaced = True
        else:
            new_workflows.append(wf)
    if not replaced:
        new_workflows.append(new_wf)
    updated["workflows"] = new_workflows
    return updated


def _serialize_workflow_yaml(wf: dict) -> str:
    """Serialize a single workflow dict to compact YAML (evidence stripped)."""
    wf_copy = dict(wf)
    new_states = []
    for st in wf_copy.get("states", []):
        st_copy = dict(st)
        new_trans = []
        for tr in st_copy.get("transitions", []):
            new_trans.append({k: v for k, v in tr.items() if k != "evidence"})
        st_copy["transitions"] = new_trans
        new_states.append(st_copy)
    wf_copy["states"] = new_states
    return yaml.dump(wf_copy, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── Targeted multi-query retrieval ──────────────────────────────────────────

# Per-workflow search queries targeting the *actual* implementation functions,
# not spec-level concepts.  Calibrated against real function names found by
# scanning all 5 clients.  Replace the old single "initial sync" query.
_WF_SEARCH_QUERIES: dict[str, list[str]] = {
    "initial_sync": [
        "range sync batch download blocks by range request",
        "forward sync peer selection suitability score",
        "batch processing import validate apply fork choice",
        "blob sidecar by range request download deneb",
        "initial sync complete set forward synced",
        "stall detection batch timeout backoff retry",
        "peer penalty downscore ban invalid batch",
        "chain reorg during sync reset clear caches",
    ],
    "regular_sync": [
        "gossip block receive handler validate import",
        "blob sidecar gossip receive validate deneb",
        "chain reorg detection handle reorganize head",
        "missing parent unknown block request by root",
        "fork choice update notify after block import",
        "attestation buffer pending block not yet imported",
        "peer score penalty invalid gossip message",
        "fallback range sync head slot lag behind",
    ],
    "checkpoint_sync": [
        "checkpoint sync anchor state initialize store",
        "weak subjectivity validation check period boundary",
        "backfill sync historical blocks genesis",
        "anchor state root hash verify finalized",
        "checkpoint source fetch finalized state block",
        "forward sync after checkpoint anchor init",
        "backfill completion data availability boundary",
    ],
    "attestation_generate": [
        "attester duty committee slot assignment fetch",
        "attestation data source target head beacon",
        "slashing protection check before sign attestation",
        "sign attestation BLS key validator",
        "publish submit attestation subnet topic",
        "attestation timing wait one third slot",
        "electra single attestation format fork aware",
    ],
    "block_generate": [
        "proposer duty block proposal slot fetch",
        "engine forkchoiceUpdated payload attributes trigger",
        "engine getPayload local payload retrieve",
        "beacon block body assemble attestations deposits slashings",
        "sign block proposer key slashing protection",
        "broadcast publish signed block gossip",
        "MEV boost builder bid external circuit breaker",
        "blob KZG commitment sidecar attach deneb",
    ],
    "aggregate": [
        "aggregator selection proof VRF compute",
        "committee attestation subnet subscription",
        "collect unaggregated attestations wait two thirds slot",
        "aggregate and proof BLS combine signatures",
        "publish submit aggregate and proof global topic",
        "selection proof is aggregator check",
        "empty aggregate skip no attestations collected",
    ],
    "execute_layer_relation": [
        "engine newPayload execution payload validate",
        "payload status VALID INVALID SYNCING handle",
        "forkchoiceUpdated head safe finalized notify",
        "optimistic import block EL syncing state",
        "invalid payload invalidate descendants rollback chain",
        "optimistic sync depth limit exceeded threshold",
        "engine API connection timeout reconnect",
        "blob versioned hashes validate deneb newPayloadV3",
    ],
}

_MAX_CODE_CONTEXT_CHARS: int = 24_000   # increased from 12k
_MAX_SNIPPETS: int = 30                  # increased from 15
_SNIPPET_CHARS: int = 600


def _retrieve_code_context(
    client_name: str,
    workflow_id: str,
    iteration: int,
    prev_wf: dict | None,
) -> list[dict]:
    """Multi-query retrieval returning snippets with stable evidence IDs.

    Returns dicts with keys: id, file, function, start_line, end_line, code.
    The stable ``id`` (client::wf::Sn) is the anchor the LLM must cite as
    evidence; grounding validation rejects any file path not in this pool.
    """
    try:
        from tools.search import search_codebase_by_workflow
    except ImportError:
        logger.debug("[_retrieve_code_context] search tools not available")
        return []

    snippets: list[dict] = []
    seen: set[tuple] = set()   # (file_path, start_line)
    total_chars = 0

    def _add(results: list, label: str = "") -> None:
        nonlocal total_chars
        for r in results:
            if total_chars >= _MAX_CODE_CONTEXT_CHARS:
                return
            fp = r.metadata.get("file_path", "")
            sl = r.metadata.get("start_line", 0)
            key = (fp, sl)
            if key in seen:
                continue
            seen.add(key)
            code = (r.content or "")[:_SNIPPET_CHARS]
            ev_id = f"{client_name}::{workflow_id}::S{len(snippets) + 1}"
            snippets.append({
                "id": ev_id,
                "file": fp,
                "function": r.metadata.get("function_name", ""),
                "start_line": sl,
                "end_line": r.metadata.get("end_line", 0),
                "code": code,
            })
            total_chars += len(code)

    # ── Targeted queries (deterministic, per-workflow) ───────────────────
    for query in _WF_SEARCH_QUERIES.get(workflow_id, []):
        if total_chars >= _MAX_CODE_CONTEXT_CHARS:
            break
        try:
            results = search_codebase_by_workflow(
                workflow_id=workflow_id,
                query=query,
                client_name=client_name,
                top_k=6,
            )
            _add(results, query)
        except Exception:
            logger.debug("[_retrieve_code_context] query failed q=%s", query, exc_info=True)

    # ── Verification queries: guard/action names from previous iteration ──
    # Only search terms that appeared in the previous LSG iteration so we can
    # verify them against real code; skip if they were obviously invented
    # (PascalCase names not found in the query corpus rarely resolve).
    if prev_wf and iteration > 1 and total_chars < _MAX_CODE_CONTEXT_CHARS:
        terms: set[str] = set()
        for st in prev_wf.get("states", []):
            for tr in st.get("transitions", []):
                g = tr.get("guard", "")
                if g and g not in ("TRUE", "*"):
                    terms.add(g)
                for a in tr.get("actions", []):
                    if a:
                        terms.add(a)
        for term in list(terms)[:8]:
            if total_chars >= _MAX_CODE_CONTEXT_CHARS:
                break
            try:
                results = search_codebase_by_workflow(
                    workflow_id=workflow_id,
                    query=term,
                    client_name=client_name,
                    top_k=3,
                )
                _add(results, f"verify:{term}")
            except Exception:
                pass

    logger.info(
        "[_retrieve_code_context] client=%s wf=%s iter=%d — %d snippets (%d chars)",
        client_name, workflow_id, iteration, len(snippets), total_chars,
    )
    return snippets[:_MAX_SNIPPETS]


# ── Evidence grounding ───────────────────────────────────────────────────────


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").strip().lower()


def _build_evidence_pools(
    snippets: list[dict], client_name: str
) -> tuple[set[str], set[str], dict[str, str]]:
    """Build path lookup sets from retrieved snippets.

    Returns:
        full_paths  – lowercased relative file paths as returned by the index
        basenames   – file basenames only (for partial match fallback)
        path_map    – lowercase path → snippet id (for resolving evidence)
    """
    full_paths: set[str] = set()
    basenames: set[str] = set()
    path_map: dict[str, str] = {}

    for s in snippets:
        fp = _norm_path(s.get("file", ""))
        if not fp:
            continue
        full_paths.add(fp)
        bn = fp.rsplit("/", 1)[-1]
        basenames.add(bn)
        path_map[fp] = s["id"]
        path_map[bn] = s["id"]

    # Also accept paths that exist on disk under code/{client}/ even if not
    # retrieved (handles cases where the LLM cites a sibling file).
    client_code = CODE_BASE_PATH / client_name
    return full_paths, basenames, path_map


def _evidence_is_real(
    ev: dict | None,
    full_paths: set[str],
    basenames: set[str],
    client_name: str,
) -> bool:
    """Return True if the evidence file is in the retrieved snippet pool OR
    actually exists on disk under code/{client_name}/."""
    if not ev or not ev.get("file"):
        return False
    fp = _norm_path(ev["file"])
    bn = fp.rsplit("/", 1)[-1]
    if fp in full_paths or bn in basenames:
        return True
    # Final safety net: check disk existence
    disk_path = CODE_BASE_PATH / client_name / ev["file"]
    return disk_path.exists()


def _ground_workflow(
    wf: dict,
    snippets: list[dict],
    client_name: str,
) -> tuple[dict, int, int]:
    """Replace hallucinated evidence with None; keep real evidence as-is.

    Returns (grounded_workflow, kept_count, cleared_count).
    """
    full_paths, basenames, _ = _build_evidence_pools(snippets, client_name)
    kept = cleared = 0
    for st in wf.get("states", []):
        for tr in st.get("transitions", []):
            ev = tr.get("evidence")
            if ev is None:
                continue
            if _evidence_is_real(ev, full_paths, basenames, client_name):
                kept += 1
            else:
                tr["evidence"] = None
                cleared += 1
    return wf, kept, cleared


# ── Agent builder ────────────────────────────────────────────────────────────


def build_phase2_sub_agent(client_name: str, llm=None, callbacks=None):
    """Build a Phase 2 Sub-Agent for *client_name*.

    If *llm* is None, returns a mock implementation.
    """
    lang_key, _ = LANGUAGE_GRAMMARS[client_name]

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        guards = state.get("guards", [])
        actions = state.get("actions", [])
        iteration = state.get("phase2_iteration", 1)
        current_wf = state.get("current_workflow", "")

        existing_lsg = state.get("client_lsgs", {}).get(client_name, {})

        all_feedback = state.get("a_class_feedback", [])
        a_class_feedback = [
            fb for fb in all_feedback
            if client_name in fb.get("involved_clients", [])
            and fb.get("workflow_id") == current_wf
        ]

        vocab = summarize_vocab_for_prompt(guards, actions, max_full_entries=80)

        previous_wf_yaml: str | None = None
        prev_wf = _extract_workflow(existing_lsg, current_wf)
        if prev_wf and iteration > 1:
            previous_wf_yaml = _serialize_workflow_yaml(prev_wf)
            logger.info(
                "[phase2_sub_agent] client=%s wf=%s — feeding back previous "
                "workflow (%d lines)",
                client_name, current_wf, previous_wf_yaml.count("\n"),
            )
        elif prev_wf and iteration == 1:
            previous_wf_yaml = _serialize_workflow_yaml(prev_wf)
            logger.info(
                "[phase2_sub_agent] client=%s wf=%s — using merged baseline "
                "(%d lines)",
                client_name, current_wf, previous_wf_yaml.count("\n"),
            )

        sparsity_hints = [
            h for h in state.get("sparsity_hints", [])
            if h.get("client") == client_name
            and h.get("workflow_id") == current_wf
        ]

        # ── Retrieve code snippets (program, no extra LLM call) ─────────
        code_snippets: list[dict] = []
        if llm is not None:
            code_snippets = _retrieve_code_context(
                client_name, current_wf, iteration, prev_wf,
            )

        template = _load_prompt_template()
        _prompt = template.render(
            client_name=client_name,
            language=lang_key,
            vocab=vocab,
            workflow_id=current_wf,
            a_class_feedback=a_class_feedback,
            previous_wf_yaml=previous_wf_yaml,
            iteration=iteration,
            sparsity_hints=sparsity_hints,
            code_snippets=code_snippets,
        )

        if llm is not None:
            try:
                chain = llm.with_structured_output(LSGFile)
                lsg: LSGFile = invoke_with_retry(
                    chain, _prompt, label=f"phase2_sub/{client_name}/{current_wf}",
                    callbacks=callbacks,
                )
                lsg_dict = lsg.model_dump()
                new_wf = _extract_workflow(lsg_dict, current_wf)
                if new_wf is None and lsg_dict.get("workflows"):
                    new_wf = lsg_dict["workflows"][0]
                    new_wf["id"] = current_wf

                if new_wf:
                    # Ground evidence: clear any hallucinated file paths
                    new_wf, kept, cleared = _ground_workflow(
                        new_wf, code_snippets, client_name,
                    )
                    if cleared:
                        logger.info(
                            "[phase2_sub_agent] client=%s wf=%s — "
                            "grounded evidence: kept=%d cleared=%d",
                            client_name, current_wf, kept, cleared,
                        )
                    updated_lsg = _replace_workflow(existing_lsg, current_wf, new_wf)
                    updated_lsg["generated_at"] = datetime.now(timezone.utc).isoformat()
                    return {"client_lsgs": {client_name: updated_lsg}}

                logger.warning(
                    "[phase2_sub_agent] LLM returned no workflow for %s/%s",
                    client_name, current_wf,
                )
            except Exception:
                logger.error(
                    "LLM call failed for %s/%s", client_name, current_wf,
                    exc_info=True,
                )

        # ── Mock fallback ────────────────────────────────────────────────
        logger.info(
            "[phase2_sub_agent] client=%s wf=%s — using mock response",
            client_name, current_wf,
        )
        mock_wf = {
            "id": current_wf,
            "name": current_wf.replace("_", " ").title(),
            "description": f"Mock {current_wf} workflow for {client_name}",
            "mode": "mock",
            "initial_state": f"{current_wf}.init",
            "states": [
                {
                    "id": f"{current_wf}.init",
                    "label": "Init",
                    "category": "init",
                    "transitions": [{"guard": "TRUE", "actions": [], "next_state": f"{current_wf}.done", "evidence": None}],
                },
                {"id": f"{current_wf}.done", "label": "Done", "category": "terminal", "transitions": []},
            ],
        }
        if existing_lsg:
            updated_lsg = _replace_workflow(existing_lsg, current_wf, mock_wf)
        else:
            updated_lsg = {
                "version": 1, "client": client_name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "guards": list(guards), "actions": list(actions),
                "workflows": [mock_wf],
            }
        return {"client_lsgs": {client_name: updated_lsg}}

    return _run
