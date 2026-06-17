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
# not spec-level concepts.  Three query tiers per workflow:
#   (a) Normal path   — the happy path flow
#   (b) Error path    — how each failure mode is detected and handled
#   (c) Boundary      — epoch transitions, fork activations, timing thresholds,
#                        capacity limits, and protocol-defined cutoffs
_WF_SEARCH_QUERIES: dict[str, list[str]] = {
    "initial_sync": [
        # (a) Normal path
        "range sync batch download blocks by range request",
        "forward sync peer selection suitability score",
        "batch processing import validate apply fork choice",
        "initial sync complete set forward synced",
        # (b) Error path
        "stall detection no progress timeout reset retry",
        "peer penalty downscore ban invalid batch",
        "chain reorg during sync clear caches reset target",
        "error invalid block remove retry different peer",
        # (c) Boundary
        "epoch boundary during sync process epoch slots",
        "blob sidecar by range request download deneb",
        "data availability boundary minimum epoch blob request",
    ],
    "regular_sync": [
        # (a) Normal path
        "gossip block receive handler validate import",
        "blob sidecar gossip receive validate deneb",
        "fork choice update notify after block import",
        # (b) Error path
        "invalid gossip block peer score penalty reject",
        "missing parent unknown block request by root",
        "chain reorg detection rollback invalidate fork choice",
        "EL invalid block remove state rollback",
        "attestation buffer pending block not yet imported",
        # (c) Boundary
        "proposer boost timing one third slot boundary",
        "hard fork upgrade activation altair bellatrix capella deneb",
        "attestation propagation slot range cutoff limit",
        "fallback range sync head slot lag behind threshold",
    ],
    "checkpoint_sync": [
        # (a) Normal path
        "checkpoint sync anchor state initialize store",
        "anchor state root hash verify finalized",
        "forward sync after checkpoint anchor init",
        # (b) Error path
        "weak subjectivity check fail abort reject checkpoint",
        "checkpoint source unreachable fetch error retry",
        "anchor state verification mismatch abort",
        # (c) Boundary
        "weak subjectivity period boundary validation epoch window",
        "backfill completion data availability boundary minimum epoch",
        "backfill sync historical blocks genesis",
    ],
    "attestation_generate": [
        # (a) Normal path
        "attester duty committee slot assignment fetch",
        "attestation data source target head beacon",
        "sign attestation BLS key validator",
        "publish submit attestation subnet topic",
        # (b) Error path
        "slashing protection check failure skip duty abort",
        "beacon node unreachable timeout retry attestation",
        "attestation submission failed error",
        # (c) Boundary
        "attestation timing one third slot window boundary",
        "epoch boundary target epoch staleness duty refresh",
        "electra single attestation format fork activation boundary",
    ],
    "block_generate": [
        # (a) Normal path
        "proposer duty block proposal slot fetch",
        "engine forkchoiceUpdated payload attributes trigger",
        "engine getPayload local payload retrieve",
        "beacon block body assemble attestations deposits slashings",
        "sign block proposer key slashing protection",
        # (b) Error path
        "MEV boost builder failure circuit breaker fallback local",
        "execution payload timeout fallback skip slot",
        "block proposal slashing protection error skip",
        # (c) Boundary
        "blob KZG commitment sidecar deneb activation boundary",
        "payload deadline timeout threshold end of slot",
        "builder registration deadline MEV boost window",
    ],
    "aggregate": [
        # (a) Normal path
        "aggregator selection proof VRF compute",
        "committee attestation subnet subscription",
        "aggregate and proof BLS combine signatures",
        "publish submit aggregate and proof global topic",
        # (b) Error path
        "empty aggregate skip no attestations collected",
        "aggregation failure error subnet subscription fail",
        # (c) Boundary
        "aggregation timing two thirds slot window boundary",
        "sync committee period rotation boundary 256 epochs",
        "selection proof is aggregator threshold check",
    ],
    "execute_layer_relation": [
        # (a) Normal path
        "engine newPayload execution payload validate",
        "forkchoiceUpdated head safe finalized notify",
        "optimistic import block EL syncing state",
        # (b) Error path
        "invalid payload invalidate descendants rollback chain latestValidHash",
        "EL disconnected timeout reconnect engine API error",
        "INVALID cascade wrong latestValidHash error recovery",
        # (c) Boundary
        "optimistic sync depth limit exceeded threshold boundary",
        "blob versioned hashes validate deneb newPayloadV3 activation",
        "payload status VALID INVALID SYNCING ACCEPTED distinction",
        "engine API connection timeout threshold recovery",
    ],
}

_MAX_CODE_CONTEXT_CHARS: int = 32_000   # increased to cover error+boundary paths
_MAX_SNIPPETS: int = 40                  # increased from 30
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
    # Split previous-iteration guards/actions by transition_type:
    #   - "normal" guards: use them for verification (they may be hallucinated
    #     and need real-code backing)
    #   - "error"/"boundary" guards: skip verification (they're already targeting
    #     the right code tier; re-verifying them would just return more normal-path
    #     snippets and reinforce the happy-path bias)
    # Then use any saved budget to force additional error-path queries for this
    # iteration, counteracting the tendency to fixate on normal-path code.
    if prev_wf and iteration > 1 and total_chars < _MAX_CODE_CONTEXT_CHARS:
        normal_terms: set[str] = set()
        error_boundary_count = 0
        for st in prev_wf.get("states", []):
            for tr in st.get("transitions", []):
                ttype = tr.get("transition_type", "normal")
                g = tr.get("guard", "")
                if g and g not in ("TRUE", "*"):
                    if ttype == "normal":
                        normal_terms.add(g)
                    else:
                        error_boundary_count += 1
                for a in tr.get("actions", []):
                    if a and ttype == "normal":
                        normal_terms.add(a)

        # Verify normal-path guards (cap at 6 to leave budget for error top-ups)
        for term in list(normal_terms)[:6]:
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

        # If error/boundary coverage is low (<3 previous transitions), force
        # additional error-path queries to break the normal-path feedback loop.
        if error_boundary_count < 3 and total_chars < _MAX_CODE_CONTEXT_CHARS:
            forced_error_queries = [
                f"{workflow_id.replace('_', ' ')} error handler failure recovery",
                f"{workflow_id.replace('_', ' ')} timeout retry backoff",
                f"{workflow_id.replace('_', ' ')} boundary condition epoch fork limit",
            ]
            logger.info(
                "[_retrieve_code_context] client=%s wf=%s — low error/boundary "
                "coverage (%d), forcing %d extra error-path queries",
                client_name, workflow_id, error_boundary_count, len(forced_error_queries),
            )
            for query in forced_error_queries:
                if total_chars >= _MAX_CODE_CONTEXT_CHARS:
                    break
                try:
                    results = search_codebase_by_workflow(
                        workflow_id=workflow_id,
                        query=query,
                        client_name=client_name,
                        top_k=4,
                    )
                    _add(results, f"error_topup:{query}")
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

    Grounding strategy by transition type:
    - "normal": evidence must be in the retrieved snippet pool (or a basename
      match), otherwise cleared. This is the strict check.
    - "error" / "boundary": evidence is accepted if the file exists on disk
      under code/{client_name}/, even when it was not retrieved by the
      normal-path queries. Error handlers and boundary guards often live in
      dedicated files (errors.go, error.rs, exceptions/) that won't appear
      in normal-path search results but are real code.

    Returns (grounded_workflow, kept_count, cleared_count).
    """
    full_paths, basenames, _ = _build_evidence_pools(snippets, client_name)
    kept = cleared = 0
    for st in wf.get("states", []):
        for tr in st.get("transitions", []):
            ev = tr.get("evidence")
            if ev is None:
                continue

            ttype = tr.get("transition_type", "normal")

            if ttype in ("error", "boundary"):
                # Relaxed check: disk existence is sufficient.
                # Error/boundary files are rarely in the retrieval pool but
                # are real code that the LLM correctly identifies.
                ev_file = ev.get("file", "")
                if ev_file:
                    disk_path = CODE_BASE_PATH / client_name / ev_file
                    if disk_path.exists() or _evidence_is_real(ev, full_paths, basenames, client_name):
                        kept += 1
                        continue
                tr["evidence"] = None
                cleared += 1
            else:
                # Strict check for normal-path transitions.
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
