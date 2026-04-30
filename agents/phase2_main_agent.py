"""Phase 2 Main Agent.

Horizontal comparison of client LSGs.  Classifies differences as A-class
(vocabulary / implementation) or B-class (structural / logic), computes
logic_diff_rate.  A-class diffs produce vocabulary-alignment directives
that are fed back to Sub-Agents; B-class diffs are preserved for human audit.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from jinja2 import Template

from config import CLIENT_NAMES, WORKFLOW_IDS
from state import DiffItem, DiffReport
from utils import compute_lsg_sparsity, invoke_with_retry

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase2_main.j2"


def _load_prompt_template() -> Template:
    return Template(_PROMPT_PATH.read_text(encoding="utf-8"))


# ── Comparison helpers ──────────────────────────────────────────────────


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity of two sets.  Returns 1.0 when both are empty."""
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _next_cat(state_id: str) -> str:
    """Extract the trailing phase token from a state id.

    ``'initial.peer_select'`` → ``'peer_select'``
    ``'done'``                → ``'done'``
    """
    return state_id.rsplit(".", 1)[-1] if "." in state_id else state_id


_MATCH_THRESHOLD = 0.45  # minimum similarity score to consider A-class


def _transition_similarity(
    guard_a: str, actions_a: frozenset, next_a: str,
    guard_b: str, actions_b: frozenset, next_b: str,
) -> float:
    """Score how similar two transitions are (0.0 – 1.0).

    Weights: guard name 30 %, action Jaccard 45 %, destination category 25 %.
    """
    g_score = 1.0 if guard_a == guard_b else 0.0
    a_score = _jaccard(set(actions_a), set(actions_b))
    n_score = 1.0 if next_a == next_b else 0.0
    return 0.30 * g_score + 0.45 * a_score + 0.25 * n_score


def _make_rename_description(
    client: str,
    guard_a: str, guard_b: str,
    actions_a: frozenset, actions_b: frozenset,
) -> str:
    """Build a human-readable rename directive for an A-class diff."""
    parts: list[str] = []
    if guard_a != guard_b:
        parts.append(f"rename guard `{guard_b}` → `{guard_a}`")
    # Detect per-action renames by pairing actions that only appear in one side
    only_a = sorted(actions_a - actions_b)
    only_b = sorted(actions_b - actions_a)
    for old, new in zip(only_b, only_a):
        parts.append(f"rename action `{old}` → `{new}`")
    # Extra actions with no 1:1 pair
    for extra in only_b[len(only_a):]:
        parts.append(f"rename action `{extra}` → ? (no canonical match)")
    if not parts:
        return f"In {client}: names already aligned"
    return f"In {client}: " + "; ".join(parts)


def _build_evidence_map(client_evidence: dict[str, Any]) -> dict:
    """Build an ``{client_name: Evidence}`` dict from raw evidence values.

    Accepts a mapping of ``{client_name: evidence_value}`` where each value
    is either a dict (from the LSG transition), ``None``, or already absent.
    Non-null entries are included in the output.
    """
    result: dict[str, Any] = {}
    for client, ev in client_evidence.items():
        if ev is not None and isinstance(ev, dict) and ev.get("file"):
            result[client] = ev
    return result


def _backfill_evidence_from_lsgs(
    diffs: list[dict],
    client_lsgs: dict[str, dict],
) -> None:
    """Backfill empty evidence fields in diffs using client LSG transitions.

    For each diff, look up the workflow/state/guard in the source client LSGs
    and copy the evidence from the first matching transition.
    """
    # Build a lookup: (client, wf_id, guard) → evidence
    ev_lookup: dict[tuple[str, str, str], dict] = {}
    for client, lsg in client_lsgs.items():
        for wf in lsg.get("workflows", []):
            wf_id = wf.get("id", "")
            for st in wf.get("states", []):
                for tr in st.get("transitions", []):
                    guard = tr.get("guard", "TRUE")
                    ev = tr.get("evidence")
                    if ev and isinstance(ev, dict) and ev.get("file"):
                        key = (client, wf_id, guard)
                        if key not in ev_lookup:
                            ev_lookup[key] = ev

    for diff in diffs:
        if diff.get("evidence"):
            continue  # already has evidence
        wf_id = diff.get("workflow_id", "")
        guard = diff.get("transition_guard", "")
        involved = diff.get("involved_clients", [])
        ev_map: dict[str, Any] = {}
        for client in involved:
            key = (client, wf_id, guard)
            if key in ev_lookup:
                ev_map[client] = ev_lookup[key]
        if ev_map:
            diff["evidence"] = ev_map


def _classify_severity(diff: dict) -> str:
    """Classify a B-class diff by severity with **security focus**.

    Returns one of: ``"CRITICAL"``, ``"MAJOR"``, ``"MINOR"``.

    The classification prioritises security-relevant divergences:
    * CRITICAL — missing safety guards, acceptance/rejection split, DoS vector
    * MAJOR — exploitable asymmetry (peer penalty, timeout, unique state)
    * MINOR — architectural / granularity difference with limited attack surface
    """
    desc_lower = (diff.get("description", "") or "").lower()
    state_id = diff.get("state_id", "")
    guard = (diff.get("transition_guard", "") or "").lower()
    sec_note = (diff.get("security_note", "") or "").lower()
    combined = desc_lower + " " + sec_note

    # ── CONSENSUS VULN tag → always CRITICAL ────────────────────────────
    if "[consensus vuln]" in sec_note:
        return "CRITICAL"

    # ── CRITICAL indicators ─────────────────────────────────────────────
    # Entire workflow missing / stub
    if state_id.endswith(".*") or "stub" in desc_lower or "missing workflow" in desc_lower:
        return "CRITICAL"
    # State category entirely absent
    if "missing in" in desc_lower and "state category" in desc_lower:
        return "CRITICAL"
    # Safety guard missing — could cause slashing or invalid state acceptance
    critical_guard_keywords = [
        "slashable", "slashing", "weak subjectivity", "finality",
        "accepts", "rejects", "consensus split", "consensus failure",
        # Consensus vulnerability patterns
        "race condition", "data race", "toctou", "time-of-check",
        "missing validation", "bypass validation", "skip verification",
        "missing bls", "signature not verified", "unsigned",
        "double vote", "double proposal", "equivocation",
        "invalid chain", "incorrect invalidation", "wrong latestvali",
        "state corruption", "inconsistent state", "atomicity",
        "unbounded allocation", "oom", "out of memory",
        "infinite loop", "infinite retry",
        "fork from honest", "chain split",
    ]
    if any(kw in combined for kw in critical_guard_keywords):
        return "CRITICAL"

    # ── MAJOR indicators ────────────────────────────────────────────────
    major_keywords = [
        # Peer penalty divergence → eclipse attack surface
        "ban", "peer penalty", "penalize", "peer score", "disconnect",
        "eclipse",
        # Timeout / stall → DoS / liveness attack
        "stall", "timeout", "recovery", "backoff", "retry",
        "dos", "denial of service", "liveness",
        # Fork-choice / chain view divergence
        "reorg", "reorgani", "fork choice", "fork-choice", "rollback",
        "invalidat", "invalid payload", "cascade",
        # Unique state → missing defense or unique bug
        "only one client", "unique", "only in", "does not feature",
        "do not have", "do not model", "doesn't have", "doesn't model",
        "absent", "not present", "not modeled", "not explicitly",
        "not featured", "lacks", "no equivalent", "present in",
        # Optimistic sync divergence
        "optimistic", "depth limit", "sync limit",
        # Blob / data availability
        "blob", "data availability", "kzg",
        # Fundamental design difference affecting behavior
        "fundamental", "architectural",
    ]
    if any(kw in combined for kw in major_keywords):
        return "MAJOR"

    # Check if only a minority of clients deviates (1-2 out of 5)
    deviating = diff.get("deviating_clients", [])
    involved = diff.get("involved_clients", [])
    if deviating and len(deviating) <= 2 and len(involved) >= 4:
        return "MAJOR"  # Minority deviation → likely exploitable

    # ── MINOR: everything else ──────────────────────────────────────────
    return "MINOR"


def _infer_deviating_clients(diff: dict) -> list[str]:
    """Infer deviating (minority) clients from the description text.

    LLM-generated B-class diffs often contain phrases like:
    * "Lighthouse and Lodestar model X. Prysm, Grandine, and Teku use Y."
    * "Prysm's LSG includes an explicit state X. Other clients ..."
    * "Teku handles the reorg logic inline ... making it less explicit"

    This function attempts to identify the minority group.
    """
    if diff.get("deviating_clients"):
        return diff["deviating_clients"]  # Already set

    desc = diff.get("description", "")
    involved = diff.get("involved_clients", [])
    if not desc or len(involved) < 3:
        return []

    from config import CLIENT_NAMES

    desc_lower = desc.lower()

    # Strategy: find which clients are named in the "contrast" clause
    # Patterns:  "ClientA ... in contrast/unlike/however/whereas, ClientB ..."
    #            "ClientA's ... Other clients ..."
    #            "ClientA does not ... Other clients do ..."
    contrast_markers = [
        " in contrast", " unlike ", " however", " whereas ",
        " on the other hand", " does not ", " doesn't ", " do not ",
        " don't ", "other clients", " less explicit", " not explicitly",
        " absence ", " lacks ", " absent ", " missing ",
    ]

    # Find the position of the first contrast marker
    marker_pos = len(desc_lower)
    found_marker = ""
    for marker in contrast_markers:
        pos = desc_lower.find(marker)
        if pos != -1 and pos < marker_pos:
            marker_pos = pos
            found_marker = marker

    if marker_pos >= len(desc_lower):
        # No contrast marker found — can't infer
        return []

    # Clients mentioned before the marker vs after
    before = desc_lower[:marker_pos]
    after = desc_lower[marker_pos:]

    clients_before: list[str] = []
    clients_after: list[str] = []
    for c in CLIENT_NAMES:
        if c.lower() in before:
            clients_before.append(c)
        if c.lower() in after:
            clients_after.append(c)

    # The minority group is the deviating set
    if clients_before and clients_after:
        if len(clients_before) <= len(clients_after):
            return sorted(clients_before)
        else:
            return sorted(clients_after)

    # Pattern: "ClientX's LSG includes ... Other clients ..."
    # → ClientX is unique (deviating)
    if "other clients" in after.lower() and clients_before:
        return sorted(clients_before)

    # Fallback: check if one client is singled out
    if len(clients_before) == 1 and not clients_after:
        return clients_before

    return []


# ── Builder ─────────────────────────────────────────────────────────────


def build_phase2_main_agent(llm=None, callbacks=None):
    """Build the Phase 2 Main Agent.

    If *llm* is None, returns a deterministic comparison implementation.
    """

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        """Compare client LSGs for ONE workflow, classify diffs."""
        client_lsgs = state.get("client_lsgs", {})
        iteration = state.get("phase2_iteration", 1)
        current_wf = state.get("current_workflow", "")
        guards = state.get("guards", [])
        actions = state.get("actions", [])

        # Compute per-client sparsity hints (scoped to current workflow)
        sparsity_hints = compute_lsg_sparsity(client_lsgs)
        sparsity_hints = [h for h in sparsity_hints if h.get("workflow_id") == current_wf]
        if sparsity_hints:
            logger.info(
                "[phase2_main_agent] %d sparse entries for workflow=%s",
                len(sparsity_hints), current_wf,
            )

        # ── LLM path ───────────────────────────────────────────────────
        if llm is not None:
            template = _load_prompt_template()
            _prompt = template.render(
                client_lsgs=client_lsgs,
                iteration=iteration,
                current_workflow=current_wf,
                guard_names=[g.get("name", "?") for g in guards],
                action_names=[a.get("name", "?") for a in actions],
            )
            try:
                chain = llm.with_structured_output(DiffReport)
                report: DiffReport = invoke_with_retry(
                    chain, _prompt, label="phase2_main",
                    callbacks=callbacks,
                )
                a_feedback = [d.model_dump() for d in report.a_class_diffs]
                # Always recompute logic_diff_rate from actual counts — the LLM
                # cannot be trusted to compute this metric correctly.
                n_a = len(report.a_class_diffs)
                n_b = len(report.b_class_diffs)
                recomputed_rate = n_b / max(n_a + n_b, 1)
                # Assign severity to B-class diffs if the LLM didn't
                b_diffs_out = []
                for d in report.b_class_diffs:
                    dd = d.model_dump()
                    # Normalize client names to lowercase (LLM may use "Lighthouse" vs "lighthouse")
                    dd["involved_clients"] = [c.lower() for c in dd.get("involved_clients", [])]
                    dd["deviating_clients"] = [c.lower() for c in dd.get("deviating_clients", [])]
                    # Infer deviating_clients from description if not set
                    if not dd.get("deviating_clients"):
                        dd["deviating_clients"] = _infer_deviating_clients(dd)
                    # Re-classify severity with security-focused rules
                    dd["severity"] = _classify_severity(dd)
                    b_diffs_out.append(dd)
                # Backfill evidence from client LSGs for diffs missing it
                _backfill_evidence_from_lsgs(b_diffs_out, client_lsgs)
                a_diffs_out = [d.model_dump() for d in report.a_class_diffs]
                # Normalize client names in A-class diffs too
                for ad in a_diffs_out:
                    ad["involved_clients"] = [c.lower() for c in ad.get("involved_clients", [])]
                    ad["deviating_clients"] = [c.lower() for c in ad.get("deviating_clients", [])]
                _backfill_evidence_from_lsgs(a_diffs_out, client_lsgs)
                return {
                    "diff_report": {
                        "a_class_diffs": a_diffs_out,
                        "b_class_diffs": b_diffs_out,
                        "logic_diff_rate": recomputed_rate,
                        "total_transitions": report.total_transitions or (n_a + n_b),
                    },
                    "logic_diff_rate": recomputed_rate,
                    "a_class_feedback": a_feedback,
                    "a_class_count": n_a,
                    "sparsity_hints": sparsity_hints,
                }
            except Exception:
                logger.error("LLM call failed for phase2_main", exc_info=True)

        # ── Deterministic comparison fallback ───────────────────────────
        logger.info(
            "[phase2_main_agent] deterministic comparison for workflow=%s (%d clients, iter=%d)",
            current_wf, len(client_lsgs), iteration,
        )
        return _deterministic_compare(client_lsgs, sparsity_hints, current_wf)

    return _run


def _deterministic_compare(
    client_lsgs: dict[str, dict],
    sparsity_hints: list[dict],
    current_wf: str = "",
) -> dict[str, Any]:
    """Deterministic A/B diff when no LLM is available.

    When *current_wf* is non-empty, only that single workflow is compared.
    Otherwise all workflows are compared (legacy behaviour).

    Comparison is done per ``(workflow_id, state_category)`` group.
    Within each group, transitions are matched across clients in three
    passes:

    1. **Exact match** — same guard name AND same action set.
    2. **Similarity match** — score ≥ threshold on (guard, Jaccard(actions),
       destination category).  Covers A1 (guard rename) and A2 (action rename).
    3. **Positional fallback** — unmatched transitions on *both* sides are
       paired 1-to-1 by position.  Covers A3 (all three names differ) since
       they occupy the same structural slot in the same state category.

    Anything still unmatched after all three passes is B-class.
    """
    a_diffs: list[dict] = []
    b_diffs: list[dict] = []
    total_items = 0
    all_clients = set(client_lsgs.keys())

    # Determine which workflow IDs to compare
    target_wf_ids = [current_wf] if current_wf else WORKFLOW_IDS

    # ── 1. Index: wf_id → state_cat → client → [(guard, actions_fs, next_cat, evidence)]
    wf_cat_idx: dict[str, dict[str, dict[str, list[tuple]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for client, lsg in client_lsgs.items():
        for wf in lsg.get("workflows", []):
            wf_id = wf["id"]
            if wf_id not in target_wf_ids:
                continue
            for st in wf.get("states", []):
                cat = st.get("category", "unknown")
                for tr in st.get("transitions", []):
                    guard = tr.get("guard", "TRUE")
                    acts = frozenset(tr.get("actions", []))
                    nc = _next_cat(tr.get("next_state", ""))
                    ev = tr.get("evidence")  # Evidence dict or None
                    wf_cat_idx[wf_id][cat][client].append((guard, acts, nc, ev))

    # ── 2. Compare within each (wf_id, cat) group ──────────────────────
    for wf_id, cat_map in wf_cat_idx.items():
        for cat, client_trs in cat_map.items():
            clients_here = set(client_trs.keys())

            # Pick reference: client with the most transitions, break
            # ties alphabetically so that the majority's vocabulary wins
            # (with equal transition counts the alphabetically-first client
            # is chosen; for real data the client with richer detail leads).
            ref_client = max(
                sorted(clients_here), key=lambda c: len(client_trs[c]),
            )
            ref_transitions = client_trs[ref_client]

            # Among clients with the SAME transition count as ref, prefer
            # the one whose guard names appear in the most other clients
            # (majority-vocabulary heuristic).
            max_trs = len(ref_transitions)
            candidates = sorted(c for c in clients_here if len(client_trs[c]) == max_trs)
            if len(candidates) > 1:
                def _vocab_overlap(c: str) -> int:
                    guards_c = {g for g, _, _, _ in client_trs[c]}
                    score = 0
                    for other_c in clients_here - {c}:
                        guards_o = {g for g, _, _, _ in client_trs[other_c]}
                        score += len(guards_c & guards_o)
                    return score
                ref_client = max(candidates, key=_vocab_overlap)
                ref_transitions = client_trs[ref_client]

            for other_client in sorted(clients_here - {ref_client}):
                other_transitions = list(client_trs[other_client])

                matched_ref: set[int] = set()
                matched_other: set[int] = set()

                # ── Pass 1: exact match ─────────────────────────────────
                for ri, (rg, ra, rn, _rev) in enumerate(ref_transitions):
                    for oj, (og, oa, on, _oev) in enumerate(other_transitions):
                        if oj in matched_other:
                            continue
                        if rg == og and ra == oa:
                            matched_ref.add(ri)
                            matched_other.add(oj)
                            break

                # ── Pass 2: similarity match (A1/A2) ───────────────────
                for ri, (rg, ra, rn, _rev) in enumerate(ref_transitions):
                    if ri in matched_ref:
                        continue
                    best_score, best_j = 0.0, -1
                    for oj, (og, oa, on, _oev) in enumerate(other_transitions):
                        if oj in matched_other:
                            continue
                        s = _transition_similarity(rg, ra, rn, og, oa, on)
                        if s > best_score:
                            best_score, best_j = s, oj
                    if best_score >= _MATCH_THRESHOLD and best_j >= 0:
                        matched_ref.add(ri)
                        matched_other.add(best_j)
                        og, oa, on, oev = other_transitions[best_j]
                        a_diffs.append({
                            "workflow_id": wf_id,
                            "state_id": f"{wf_id}.{cat}",
                            "transition_guard": og,
                            "diff_type": "A",
                            "description": _make_rename_description(
                                other_client, rg, og, ra, oa,
                            ),
                            "involved_clients": sorted([ref_client, other_client]),
                            "evidence": _build_evidence_map(
                                {ref_client: _rev, other_client: oev},
                            ),
                        })

                # ── Pass 3: positional fallback (A3) ───────────────────
                # When guard + actions + dest are ALL renamed, similarity
                # is 0.  But transitions in the same (wf_id, category) at
                # the same ordinal position are very likely the same
                # transition with completely different vocabulary.
                unmatched_ref = [
                    i for i in range(len(ref_transitions)) if i not in matched_ref
                ]
                unmatched_other = [
                    j for j in range(len(other_transitions)) if j not in matched_other
                ]
                pair_count = min(len(unmatched_ref), len(unmatched_other))
                for k in range(pair_count):
                    ri = unmatched_ref[k]
                    oj = unmatched_other[k]
                    rg, ra, rn, rev = ref_transitions[ri]
                    og, oa, on, oev = other_transitions[oj]
                    matched_ref.add(ri)
                    matched_other.add(oj)
                    a_diffs.append({
                        "workflow_id": wf_id,
                        "state_id": f"{wf_id}.{cat}",
                        "transition_guard": og,
                        "diff_type": "A",
                        "description": _make_rename_description(
                            other_client, rg, og, ra, oa,
                        ),
                        "involved_clients": sorted([ref_client, other_client]),
                        "evidence": _build_evidence_map(
                            {ref_client: rev, other_client: oev},
                        ),
                    })

                # ── Count all ref transitions as comparison items ───────
                total_items += len(ref_transitions)

                # ── Remaining unmatched ref → B-class ──────────────────
                for ri in range(len(ref_transitions)):
                    if ri in matched_ref:
                        continue
                    rg, ra, rn, rev = ref_transitions[ri]
                    diff_entry = {
                        "workflow_id": wf_id,
                        "state_id": f"{wf_id}.{cat}",
                        "transition_guard": rg,
                        "diff_type": "B",
                        "description": (
                            f"Transition ({wf_id}, {cat}, {rg}) present "
                            f"in {ref_client} but no equivalent in "
                            f"{other_client}."
                        ),
                        "involved_clients": sorted([ref_client, other_client]),
                        "deviating_clients": [other_client],
                        "evidence": _build_evidence_map({ref_client: rev}),
                    }
                    diff_entry["severity"] = _classify_severity(diff_entry)
                    b_diffs.append(diff_entry)

                # ── Remaining unmatched other → B-class ────────────────
                for oj in range(len(other_transitions)):
                    if oj in matched_other:
                        continue
                    og, oa, on, oev = other_transitions[oj]
                    total_items += 1
                    diff_entry = {
                        "workflow_id": wf_id,
                        "state_id": f"{wf_id}.{cat}",
                        "transition_guard": og,
                        "diff_type": "B",
                        "description": (
                            f"Transition ({wf_id}, {cat}, {og}) present "
                            f"in {other_client} but no equivalent in "
                            f"{ref_client}."
                        ),
                        "involved_clients": sorted([ref_client, other_client]),
                        "deviating_clients": [other_client],
                        "evidence": _build_evidence_map({other_client: oev}),
                    }
                    diff_entry["severity"] = _classify_severity(diff_entry)
                    b_diffs.append(diff_entry)

            # Clients that don't have this state category at all
            for c in sorted(all_clients - clients_here):
                for ref_g, ref_a, ref_n, ref_ev in ref_transitions:
                    total_items += 1
                    diff_entry = {
                        "workflow_id": wf_id,
                        "state_id": f"{wf_id}.{cat}",
                        "transition_guard": ref_g,
                        "diff_type": "B",
                        "description": (
                            f"State category `{cat}` in workflow `{wf_id}` "
                            f"exists in {', '.join(sorted(clients_here))} "
                            f"but is missing in {c}."
                        ),
                        "involved_clients": sorted(list(clients_here) + [c]),
                        "deviating_clients": [c],
                        "evidence": _build_evidence_map({ref_client: ref_ev}),
                    }
                    diff_entry["severity"] = _classify_severity(diff_entry)
                    b_diffs.append(diff_entry)

    # ── 3. Check for stub workflows ────────────────────────────────────
    for wf_id in target_wf_ids:
        clients_with_wf = set()
        for client, lsg in client_lsgs.items():
            for wf in lsg.get("workflows", []):
                if wf["id"] == wf_id and len(wf.get("states", [])) > 2:
                    clients_with_wf.add(client)
        missing = all_clients - clients_with_wf
        if missing and clients_with_wf:
            total_items += 1
            diff_entry = {
                "workflow_id": wf_id,
                "state_id": f"{wf_id}.*",
                "transition_guard": "*",
                "diff_type": "B",
                "description": (
                    f"Workflow `{wf_id}` is substantive in "
                    f"{', '.join(sorted(clients_with_wf))} but only a stub "
                    f"in {', '.join(sorted(missing))}."
                ),
                "involved_clients": sorted(missing | clients_with_wf),
                "deviating_clients": sorted(missing),
                "evidence": {},
            }
            diff_entry["severity"] = _classify_severity(diff_entry)
            b_diffs.append(diff_entry)

    logic_diff_rate = len(b_diffs) / max(total_items, 1)

    return {
        "diff_report": {
            "a_class_diffs": a_diffs,
            "b_class_diffs": b_diffs,
            "logic_diff_rate": logic_diff_rate,
            "total_transitions": total_items,
        },
        "logic_diff_rate": logic_diff_rate,
        "a_class_feedback": a_diffs,
        "a_class_count": len(a_diffs),
        "sparsity_hints": sparsity_hints,
    }

