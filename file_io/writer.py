"""File output writer.

Responsible for writing:
  - Global_LSG_Spec_Enriched.yaml  (Phase 1 exit)
  - LSG_<Client>_final.yaml        (Phase 2 exit × 5)
  - LSG_<Client>_iter<N>.yaml      (intermediate)
  - Audit_Diff_Report.md           (Phase 2 exit)
  - Audit_Diff_Report.json         (Phase 2 exit — structured)
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import config
from config import CLIENT_NAMES, WORKFLOW_IDS

logger = logging.getLogger(__name__)


def write_enriched_spec(state: dict[str, Any]) -> Path:
    """Write the enriched global vocabulary (Phase 1 output)."""
    config.OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_PATH / "Global_LSG_Spec_Enriched.yaml"

    spec = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "guards": list(state.get("guards", [])),
        "actions": list(state.get("actions", [])),
    }

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(spec, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logger.info("[write_enriched_spec] → %s", path)
    return path


def write_client_lsg(client_name: str, lsg: dict[str, Any], final: bool = False) -> Path:
    """Write a client's LSG YAML (intermediate or final)."""
    if final:
        config.OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
        path = config.OUTPUT_PATH / f"LSG_{client_name}_final.yaml"
    else:
        config.ITERATIONS_PATH.mkdir(parents=True, exist_ok=True)
        iteration = lsg.get("_iteration", 0)
        path = config.ITERATIONS_PATH / f"LSG_{client_name}_iter{iteration}.yaml"

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(lsg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logger.info("[write_client_lsg] client=%s final=%s → %s", client_name, final, path)
    return path


def _collect_referenced_names(workflows: list[dict]) -> tuple[set[str], set[str]]:
    """Return (guard_names, action_names) referenced in *workflows*."""
    guard_names: set[str] = set()
    action_names: set[str] = set()
    for wf in workflows:
        for st in wf.get("states", []):
            for tr in st.get("transitions", []):
                g = tr.get("guard", "")
                if g and g != "TRUE":
                    guard_names.add(g)
                for a in tr.get("actions", []):
                    if a:
                        action_names.add(a)
    return guard_names, action_names


def write_all_final_lsgs(state: dict[str, Any]) -> list[Path]:
    """Write final LSG YAML for all clients.

    If a client LSG has empty ``guards``/``actions`` (common when the LLM
    focuses on workflows and skips vocab fields), backfill from the global
    vocabulary filtered to names actually referenced in that client's
    workflow transitions.
    """
    paths: list[Path] = []
    client_lsgs = state.get("client_lsgs", {})
    global_guards = state.get("guards", [])
    global_actions = state.get("actions", [])

    for client_name in CLIENT_NAMES:
        lsg = client_lsgs.get(client_name)
        if lsg is None:
            continue

        # ── Backfill empty guards/actions from global vocabulary ─────
        if not lsg.get("guards") or not lsg.get("actions"):
            ref_guards, ref_actions = _collect_referenced_names(
                lsg.get("workflows", []),
            )
            if not lsg.get("guards") and global_guards:
                lsg["guards"] = [
                    g for g in global_guards if g.get("name") in ref_guards
                ]
                logger.info(
                    "[write_all_final_lsgs] backfilled %d guards for %s",
                    len(lsg["guards"]), client_name,
                )
            if not lsg.get("actions") and global_actions:
                lsg["actions"] = [
                    a for a in global_actions if a.get("name") in ref_actions
                ]
                logger.info(
                    "[write_all_final_lsgs] backfilled %d actions for %s",
                    len(lsg["actions"]), client_name,
                )

        path = write_client_lsg(client_name, lsg, final=True)
        paths.append(path)
    return paths


# Internal analytics helpers for the enriched report


def _count_transitions_per_workflow(
    client_lsgs: dict[str, dict],
) -> dict[str, int]:
    """Count total unique transitions per workflow across all client LSGs.

    Returns the **maximum** transition count among all clients for each
    workflow.  This represents the "reference size" of that workflow — i.e.
    how many comparison items the most-detailed client contributes.
    """
    wf_counts: dict[str, Counter] = defaultdict(Counter)
    for _client, lsg in client_lsgs.items():
        for wf in lsg.get("workflows", []):
            wf_id = wf.get("id", "?")
            n_transitions = sum(
                len(st.get("transitions", []))
                for st in wf.get("states", [])
            )
            wf_counts[wf_id][_client] = n_transitions
    # Use max across clients as the reference size for each workflow
    result: dict[str, int] = {}
    for wf_id, per_client in wf_counts.items():
        result[wf_id] = max(per_client.values()) if per_client else 0
    return result


def _per_workflow_summary(
    a_diffs: list[dict], b_diffs: list[dict],
    total_transitions: int = 0,
    client_lsgs: dict[str, dict] | None = None,
) -> list[dict]:
    """Return per-workflow summary sorted by total diff count (descending).

    ``similarity`` reflects structural agreement: the proportion of
    comparison items that are NOT B-class diffs.  A-class diffs (vocabulary
    misalignment) do not reduce similarity because they are auto-resolved.

    When *client_lsgs* is provided, per-workflow transition counts are
    computed directly from LSG data — this gives an accurate per-workflow
    denominator instead of distributing the aggregate ``total_transitions``
    proportionally (which was circular and yielded ~uniform similarity).
    """
    wf_a: Counter = Counter()
    wf_b: Counter = Counter()
    for d in a_diffs:
        wf_a[d.get("workflow_id", "?")] += 1
    for d in b_diffs:
        wf_b[d.get("workflow_id", "?")] += 1

    all_wfs = sorted(set(wf_a.keys()) | set(wf_b.keys()) | set(WORKFLOW_IDS))

    # Compute per-workflow transition counts from LSG data when available
    wf_transition_counts: dict[str, int] = {}
    if client_lsgs:
        wf_transition_counts = _count_transitions_per_workflow(client_lsgs)

    rows: list[dict] = []
    for wf in all_wfs:
        a = wf_a.get(wf, 0)
        b = wf_b.get(wf, 0)
        total = a + b
        # Use actual per-workflow transition count when available;
        # fall back to even split of total_transitions or diff count.
        wf_transitions = wf_transition_counts.get(wf, 0)
        if wf_transitions <= 0:
            # Fallback: even split or diff count
            if total_transitions > 0:
                wf_transitions = total_transitions // max(len(all_wfs), 1)
            else:
                wf_transitions = max(total, 1)
        # Floor: denominator must be at least as large as B-class count
        wf_transitions = max(wf_transitions, b, 1)
        similarity = max(1.0 - b / wf_transitions, 0.0)
        rows.append({
            "workflow_id": wf,
            "a_class": a,
            "b_class": b,
            "total": total,
            "similarity": similarity,
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def _per_client_ranking(
    a_diffs: list[dict], b_diffs: list[dict],
) -> list[dict]:
    """Return per-client deviation ranking (most deviant first).

    For B-class diffs, distinguishes between the *deviating* client(s) —
    those whose implementation diverges from the majority — and the
    reference/majority clients.  The ``b_class_deviating`` column counts
    how many B-class diffs a client is *responsible for*, whereas
    ``b_class`` counts total involvement (including as reference).

    The ranking is sorted by ``b_class_deviating`` (descending), then by
    ``total`` as tiebreaker.
    """
    # Normalize client name to lowercase to prevent case-variant duplicates
    # (e.g., LLM may output "Lighthouse" vs config's "lighthouse").
    def _norm(name: str) -> str:
        return name.lower()

    client_a: Counter = Counter()
    client_b: Counter = Counter()       # total involvement
    client_b_dev: Counter = Counter()   # deviating involvement only
    for d in a_diffs:
        for c in d.get("involved_clients", []):
            client_a[_norm(c)] += 1
    for d in b_diffs:
        deviating = [_norm(c) for c in d.get("deviating_clients", [])]
        for c in d.get("involved_clients", []):
            client_b[_norm(c)] += 1
        # If deviating_clients was populated, use it; otherwise fall back
        # to counting all involved (legacy / LLM-generated diffs).
        if deviating:
            for c in deviating:
                client_b_dev[c] += 1
        else:
            for c in d.get("involved_clients", []):
                client_b_dev[_norm(c)] += 1

    all_clients = sorted(set(client_a.keys()) | set(client_b.keys()) | set(_norm(c) for c in CLIENT_NAMES))
    rows: list[dict] = []
    for c in all_clients:
        a = client_a.get(c, 0)
        b = client_b.get(c, 0)
        b_dev = client_b_dev.get(c, 0)
        rows.append({
            "client": c,
            "a_class": a,
            "b_class": b,
            "b_class_deviating": b_dev,
            "total": a + b,
        })
    rows.sort(key=lambda r: (r["b_class_deviating"], r["total"]), reverse=True)
    return rows


def _agreement_workflows(
    a_diffs: list[dict], b_diffs: list[dict],
) -> list[str]:
    """Return workflow IDs where ALL clients fully agree (0 diffs)."""
    diff_wfs = set()
    for d in a_diffs:
        diff_wfs.add(d.get("workflow_id", "?"))
    for d in b_diffs:
        diff_wfs.add(d.get("workflow_id", "?"))
    return sorted(wf for wf in WORKFLOW_IDS if wf not in diff_wfs)


def _classify_severity_fallback(diff: dict) -> str:
    """Fallback severity classification for B-class diffs — security-focused."""
    desc_lower = (diff.get("description", "") or "").lower()
    state_id = diff.get("state_id", "")
    sec_note = (diff.get("security_note", "") or "").lower()
    combined = desc_lower + " " + sec_note

    if state_id.endswith(".*") or "stub" in desc_lower:
        return "CRITICAL"
    if "missing in" in desc_lower and "state category" in desc_lower:
        return "CRITICAL"

    # Safety-critical keywords → CRITICAL
    critical_kw = ["slashing", "slashable", "consensus split", "finality", "accepts", "rejects"]
    if any(kw in combined for kw in critical_kw):
        return "CRITICAL"

    # Security-relevant → MAJOR
    major_kw = [
        "ban", "peer penalty", "penalize", "eclipse",
        "stall", "timeout", "dos", "liveness",
        "reorg", "fork choice", "invalidat", "cascade",
        "optimistic", "depth limit", "blob", "unique",
        "only one client", "not explicitly", "absent",
        "fundamental", "architectural", "not modeled",
    ]
    if any(kw in combined for kw in major_kw):
        return "MAJOR"

    return "MINOR"


# Canonical severity values.
_VALID_SEVERITIES = {"CRITICAL", "MAJOR", "MINOR"}

# Map common non-standard LLM outputs to canonical values (case-insensitive).
_SEVERITY_ALIASES: dict[str, str] = {
    "high": "MAJOR",
    "medium": "MINOR",
    "low": "MINOR",
    "severe": "CRITICAL",
    "critical": "CRITICAL",
    "major": "MAJOR",
    "minor": "MINOR",
}


def _normalize_severity(diff: dict) -> str:
    """Return a canonical severity for *diff*, normalizing LLM quirks.

    If the value is already canonical (``CRITICAL``/``MAJOR``/``MINOR``),
    return it as-is.  Otherwise try alias lookup, then fall back to the
    heuristic classifier.
    """
    raw = (diff.get("severity") or "").strip()
    if raw in _VALID_SEVERITIES:
        return raw
    mapped = _SEVERITY_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    return _classify_severity_fallback(diff)


# ── Audit-review remediation: cross-list consistency helpers ──────────


def _diff_key(diff: dict) -> tuple:
    """Stable identity for a B-class diff across kept / rejected / dropped
    / reclassified lists.

    Used to enforce the invariant: a diff that ended up in the kept
    ``b_class_diffs`` set MUST NOT also appear in the FP / dropped /
    reclassified lists. This invariant can otherwise be violated because
    those lists use additive LangGraph reducers and accumulate across
    Phase 2 ``b_iter`` re-runs (a diff can be dropped in iteration N then
    re-accepted in iteration N+1, leaving a stale record in the dropped
    list).
    """
    return (
        (diff.get("workflow_id") or "").strip(),
        (diff.get("state_id") or "").strip(),
        (diff.get("transition_guard") or "").strip(),
        tuple(sorted(diff.get("deviating_clients") or [])),
    )


def _filter_excluded_against_kept(
    excluded: list[dict],
    kept_keys: set[tuple],
    bucket: str,
) -> list[dict]:
    """Drop entries from *excluded* whose key is also in *kept_keys*.

    Logs a warning per dropped stale entry.
    """
    if not excluded:
        return []
    out: list[dict] = []
    for d in excluded:
        if _diff_key(d) in kept_keys:
            logger.warning(
                "[writer] Dropping stale '%s' record superseded by kept B-diff: "
                "wf=%s state=%s guard=%s deviating=%s",
                bucket,
                d.get("workflow_id"), d.get("state_id"),
                d.get("transition_guard"), d.get("deviating_clients"),
            )
            continue
        out.append(d)
    return out


def _dedup_excluded(excluded: list[dict]) -> list[dict]:
    """Deduplicate by ``_diff_key`` (keep first occurrence)."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for d in excluded or []:
        k = _diff_key(d)
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def _evidence_tag(diff: dict) -> str:
    """Return ``full`` | ``partial`` | ``none`` based on per-client evidence."""
    ev = diff.get("evidence") or {}
    involved = diff.get("involved_clients") or []
    has_any = any(ev.get(c) for c in (involved or ev.keys()))
    if not has_any:
        return "none"
    if involved and sum(1 for c in involved if ev.get(c)) < len(involved):
        return "partial"
    return "full"


def _reconcile_verdict_lists(
    state_like_lists: dict[str, list[dict]],
    kept_b_diffs: list[dict],
) -> dict[str, list[dict]]:
    """Single source of truth: filter excluded lists against kept set and
    deduplicate. Returns a dict with the same keys as input."""
    kept_keys = {_diff_key(d) for d in kept_b_diffs}
    # Soft overlap: same (workflow, state, guard) but different
    # deviating_clients. Not a contradiction, but very confusing in a
    # human-readable report — log so it can be noticed and merged later.
    kept_soft_keys = {
        ((d.get("workflow_id") or "").strip(),
         (d.get("state_id")    or "").strip(),
         (d.get("transition_guard") or "").strip())
        for d in kept_b_diffs
    }
    out: dict[str, list[dict]] = {}
    for bucket, items in state_like_lists.items():
        out[bucket] = _dedup_excluded(
            _filter_excluded_against_kept(items, kept_keys, bucket)
        )
        for d in out[bucket]:
            soft = ((d.get("workflow_id") or "").strip(),
                    (d.get("state_id") or "").strip(),
                    (d.get("transition_guard") or "").strip())
            if soft in kept_soft_keys:
                logger.warning(
                    "[writer] Soft-overlap: '%s' entry shares "
                    "(workflow=%s, state=%s, guard=%s) with a kept B-diff "
                    "but has different deviating_clients=%s — readers may "
                    "find this confusing; consider merging upstream.",
                    bucket, *soft, d.get("deviating_clients"),
                )
    # Hard invariant — must hold after filter.
    for bucket, items in out.items():
        overlap = [d for d in items if _diff_key(d) in kept_keys]
        if overlap:
            raise AssertionError(
                f"[writer] Consistency invariant violated: {len(overlap)} "
                f"items in '{bucket}' still overlap kept b_class_diffs: "
                f"{[_diff_key(d) for d in overlap]}"
            )
    return out


def _deduplicate_b_diffs(b_diffs: list[dict]) -> list[dict]:
    """Deduplicate B-class diffs that describe the same structural difference.

    Diffs are grouped by ``(workflow_id, state_id, transition_guard)``.
    Within each group, entries whose descriptions describe opposite directions
    of the same gap (e.g. "present in X but not Y" and "present in Y but not X")
    are merged: ``involved_clients`` are unioned and descriptions concatenated.
    """
    from collections import OrderedDict

    groups: OrderedDict[tuple, list[dict]] = OrderedDict()
    for d in b_diffs:
        key = (
            d.get("workflow_id", "?"),
            d.get("state_id", "?"),
            d.get("transition_guard", "?"),
        )
        groups.setdefault(key, []).append(d)

    deduped: list[dict] = []
    for key, entries in groups.items():
        if len(entries) == 1:
            deduped.append(entries[0])
            continue

        # Merge: union involved_clients, union deviating_clients, pick highest severity, join descriptions
        merged_clients: set[str] = set()
        merged_deviating: set[str] = set()
        descriptions: list[str] = []
        security_notes: list[str] = []
        best_severity = "MINOR"
        evidence: dict = {}
        severity_rank = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1, "": 0}

        seen_descs: set[str] = set()
        for e in entries:
            for c in e.get("involved_clients", []):
                merged_clients.add(c)
            for c in e.get("deviating_clients", []):
                merged_deviating.add(c)
            desc = e.get("description", "")
            if desc and desc not in seen_descs:
                descriptions.append(desc)
                seen_descs.add(desc)
            sec = e.get("security_note", "")
            if sec and sec not in security_notes:
                security_notes.append(sec)
            sev = _normalize_severity(e)
            if severity_rank.get(sev, 0) > severity_rank.get(best_severity, 0):
                best_severity = sev
            if e.get("evidence"):
                evidence.update(e["evidence"])

        deduped.append({
            "workflow_id": key[0],
            "state_id": key[1],
            "transition_guard": key[2],
            "diff_type": "B",
            "description": " | ".join(descriptions) if len(descriptions) > 1 else (descriptions[0] if descriptions else ""),
            "severity": best_severity,
            "involved_clients": sorted(merged_clients),
            "deviating_clients": sorted(merged_deviating),
            "security_note": " | ".join(security_notes) if security_notes else "",
            "evidence": evidence,
        })

    return deduped


def _generate_executive_summary(
    a_diffs: list[dict],
    b_diffs: list[dict],
    wf_summary: list[dict],
    client_ranking: list[dict],
    agreement_wfs: list[str],
    force_stopped: bool,
) -> str:
    """Generate a human-readable executive summary paragraph (security-focused)."""
    total_a = len(a_diffs)
    total_b = len(b_diffs)
    total = total_a + total_b

    sev_counts = Counter(d.get("severity", "MAJOR") for d in b_diffs)
    n_critical = sev_counts.get("CRITICAL", 0)
    n_major = sev_counts.get("MAJOR", 0)
    n_vuln = sum(
        1 for d in b_diffs
        if "[consensus vuln]" in (d.get("security_note", "") or "").lower()
        and d.get("verdict_class") == "confirmed"
    )

    parts: list[str] = []

    parts.append(
        f"This security-focused audit compares {len(CLIENT_NAMES)} Ethereum consensus "
        f"clients ({', '.join(CLIENT_NAMES)}) across {len(WORKFLOW_IDS)} core workflows. "
        f"A total of **{total}** differences were identified: "
        f"**{total_a}** A-class (vocabulary/naming, auto-resolved) "
        f"and **{total_b}** B-class (structural logic divergences)."
    )

    if n_vuln > 0:
        parts.append(
            f"🚨 **{n_vuln} consensus-impacting vulnerabilities** were identified "
            f"that could cause chain splits, finality failures, or safety violations."
        )

    if n_critical > 0 or n_major > 0:
        parts.append(
            f"**Security assessment**: {n_critical} CRITICAL and {n_major} MAJOR "
            f"findings with potential security implications (eclipse attacks, DoS "
            f"vectors, consensus divergences)."
        )

    if agreement_wfs:
        parts.append(
            f"All clients are in **full agreement** on "
            f"{len(agreement_wfs)} workflow(s): {', '.join(f'`{w}`' for w in agreement_wfs)}."
        )

    if wf_summary:
        most_divergent = wf_summary[0]
        parts.append(
            f"The most divergent workflow is **`{most_divergent['workflow_id']}`** "
            f"with {most_divergent['total']} diffs "
            f"({most_divergent['b_class']} B-class)."
        )

    if client_ranking:
        most_unique = client_ranking[0]
        dev_count = most_unique.get("b_class_deviating", most_unique["b_class"])
        parts.append(
            f"The client with the most unique implementation choices is "
            f"**{most_unique['client']}** (deviating in {dev_count} B-class diffs)."
        )

    if force_stopped:
        parts.append(
            "⚠️ The pipeline was **force-stopped** before natural convergence."
        )

    return " ".join(parts)


# Public writers


def write_diff_report(state: dict[str, Any]) -> Path:
    """Write the Audit Diff Report (Phase 2 output) as enriched Markdown.

    Sections:
    1. Executive Summary
    2. Summary table
    3. Per-Workflow Summary
    4. Per-Client Deviation Ranking
    5. A-Class Vocabulary Alignment Diffs
    6. B-Class Structural Logic Differences (grouped by severity)
    7. Agreement (fully matching workflows)
    8. Iteration Trend (if available)
    """
    config.OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_PATH / "Audit_Diff_Report.md"

    diff_report = state.get("diff_report", {})
    b_diffs_raw = diff_report.get("b_class_diffs", [])
    a_diffs = diff_report.get("a_class_diffs", [])
    logic_diff_rate = diff_report.get("logic_diff_rate", 0.0)
    total_transitions = diff_report.get("total_transitions", 0)
    force_stopped = state.get("force_stopped", False)
    convergence_reason = state.get("convergence_reason", "")
    iteration_history = state.get("iteration_history", [])

    # ── Deduplicate B-class diffs ──────────────────────────────────────
    b_diffs = _deduplicate_b_diffs(b_diffs_raw)

    # ── Ensure all B-class diffs have canonical severity ─────────────
    for d in b_diffs:
        d["severity"] = _normalize_severity(d)

    # ── Compute analytics ──────────────────────────────────────────────
    client_lsgs = state.get("client_lsgs", {})
    wf_summary = _per_workflow_summary(a_diffs, b_diffs, total_transitions, client_lsgs)
    client_ranking = _per_client_ranking(a_diffs, b_diffs)
    agreement_wfs = _agreement_workflows(a_diffs, b_diffs)
    exec_summary = _generate_executive_summary(
        a_diffs, b_diffs, wf_summary, client_ranking, agreement_wfs, force_stopped,
    )

    lines: list[str] = [
        "# EthAuditor — Audit Diff Report",
        "",
        f"**Generated at**: {datetime.now(timezone.utc).isoformat()}",
        f"**Logic Diff Rate (B-class)**: {logic_diff_rate:.4f}",
        f"**Force Stopped**: {force_stopped}",
    ]
    if convergence_reason:
        lines.append(f"**Convergence Reason**: {convergence_reason}")
    lines.append("")

    # ── 1. Executive Summary ───────────────────────────────────────────
    lines.extend([
        "## Executive Summary",
        "",
        exec_summary,
        "",
    ])

    # ── 2. Summary Table ───────────────────────────────────────────────
    # Reconcile excluded lists against the kept set BEFORE any counting,
    # so headline numbers and the FP file agree with the main report.
    _excluded = _reconcile_verdict_lists(
        {
            "rejected_b_diffs":  state.get("rejected_b_diffs", []),
            "dropped_b_diffs":   state.get("dropped_b_diffs", []),
            "reclassified_to_a": state.get("reclassified_to_a", []),
        },
        kept_b_diffs=b_diffs,
    )
    rejected_diffs     = _excluded["rejected_b_diffs"]
    dropped_diffs      = _excluded["dropped_b_diffs"]
    reclassified_diffs = _excluded["reclassified_to_a"]
    # Mutate state so write_false_positives_report sees the same filtered
    # lists (single source of truth).
    state["rejected_b_diffs"]   = rejected_diffs
    state["dropped_b_diffs"]    = dropped_diffs
    state["reclassified_to_a"]  = reclassified_diffs

    # Count B-class by severity
    sev_counts = Counter(d.get("severity", "MAJOR") for d in b_diffs)
    # Audit-review fix: only items that survived Phase 3 + evidence gate
    # AND are explicitly classified ``confirmed`` count as consensus vulns
    # in the headline. Previously this counted every kept item whose
    # ``security_note`` mentioned ``[CONSENSUS VULN]``, including leads.
    n_vuln = sum(
        1 for d in b_diffs
        if "[consensus vuln]" in (d.get("security_note", "") or "").lower()
        and d.get("verdict_class") == "confirmed"
    )
    lines.extend([
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| A-class (vocabulary alignment) diffs | {len(a_diffs)} |",
        f"| B-class (structural logic) diffs | {len(b_diffs)} |",
        f"| — 🔴 CRITICAL | {sev_counts.get('CRITICAL', 0)} |",
        f"| — 🟠 MAJOR | {sev_counts.get('MAJOR', 0)} |",
        f"| — 🟡 MINOR | {sev_counts.get('MINOR', 0)} |",
        f"| 🚨 Consensus Vulnerabilities | {n_vuln} |",
        f"| Total diffs | {len(a_diffs) + len(b_diffs)} |",
        f"| Workflows with full agreement | {len(agreement_wfs)} |",
        "",
    ])

    # ── 2b. Verification Summary (Phase 3) ─────────────────────────────
    n_confirmed = sum(1 for d in b_diffs if d.get("verification_status") == "CONFIRMED")
    n_downgraded = sum(1 for d in b_diffs if d.get("verification_status") == "DOWNGRADED")
    has_verification = (
        n_confirmed + n_downgraded + len(rejected_diffs) + len(reclassified_diffs) > 0
    )
    if has_verification:
        lines.extend([
            "## Verification Summary (Phase 3)",
            "",
            "Each B-class diff was verified against source code. "
            "Only **verified** diffs appear in the report below.",
            "",
            "| Verdict | Count |",
            "|---------|-------|",
            f"| ✅ Confirmed | {n_confirmed} |",
            f"| ⬇️ Downgraded | {n_downgraded} |",
            f"| ❌ Rejected (false positive) | {len(rejected_diffs)} |",
            f"| 🔄 Reclassified (B→A) | {len(reclassified_diffs)} |",
            "",
        ])

    # ── 3. Per-Workflow Summary ────────────────────────────────────────
    lines.extend([
        "## Per-Workflow Summary",
        "",
        "| Workflow | A-class | B-class | Total | Similarity |",
        "|----------|---------|---------|-------|------------|",
    ])
    for row in wf_summary:
        lines.append(
            f"| `{row['workflow_id']}` | {row['a_class']} | {row['b_class']} "
            f"| {row['total']} | {row['similarity']:.0%} |"
        )
    lines.append("")

    # ── 4. Per-Client Deviation Ranking ────────────────────────────────
    lines.extend([
        "## Per-Client Deviation Ranking",
        "",
        "| Client | A-class | B-class (involved) | B-class (deviating) | Total |",
        "|--------|---------|--------------------|--------------------|-------|",
    ])
    for row in client_ranking:
        lines.append(
            f"| **{row['client']}** | {row['a_class']} | {row['b_class']} "
            f"| {row.get('b_class_deviating', row['b_class'])} "
            f"| {row['total']} |"
        )
    lines.append("")

    # ── 5. A-Class Vocabulary Alignment Diffs ──────────────────────────
    if a_diffs:
        lines.extend([
            "## A-Class Vocabulary Alignment Diffs",
            "",
            "These differences reflect **naming misalignment only** — the "
            "underlying logic is equivalent. The system has generated rename "
            "directives to align vocabulary across clients.",
            "",
        ])
        for i, diff in enumerate(a_diffs, 1):
            lines.append(
                f"### A-{i}: {diff.get('workflow_id', '?')} / "
                f"{diff.get('state_id', '?')}"
            )
            lines.append("")
            lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
            lines.append(
                f"- **Clients involved**: "
                f"{', '.join(diff.get('involved_clients', []))}"
            )
            lines.append(f"- **Rename directive**: {diff.get('description', '')}")
            tag = _evidence_tag(diff)
            if tag != "full":
                lines.append(
                    f"- **Evidence**: ⚠️ `{tag}` — this rename directive is "
                    "not fully backed by per-client source citations."
                )
            lines.append("")
    else:
        lines.extend([
            "## A-Class Vocabulary Alignment Diffs",
            "",
            "✅ No vocabulary misalignment detected — all clients use "
            "consistent guard and action names.",
            "",
        ])

    # ── 6. B-Class Structural Logic Differences (by severity) ──────────
    if b_diffs:
        # Separate consensus vulnerabilities from cross-client divergences
        vuln_diffs = [d for d in b_diffs if "[consensus vuln]" in (d.get("security_note", "") or "").lower()]
        divergence_diffs = [d for d in b_diffs if "[consensus vuln]" not in (d.get("security_note", "") or "").lower()]

        # ── 6a. Consensus Vulnerabilities — bucketed by evidence-gate verdict ─
        if vuln_diffs:
            confirmed_vulns = [d for d in vuln_diffs if d.get("verdict_class") == "confirmed"]
            lead_vulns      = [d for d in vuln_diffs if d.get("verdict_class") == "lead"]
            other_vulns     = [d for d in vuln_diffs if d.get("verdict_class") not in ("confirmed", "lead")]
            # Anything still un-classified is treated as a lead (conservative).
            lead_vulns += other_vulns

            lines.extend([
                "## 🚨 Consensus-Impacting Vulnerabilities",
                "",
                "Findings are bucketed by the deterministic **evidence gate**:",
                "",
                f"- **Confirmed** ({len(confirmed_vulns)}) — production-code citation, "
                "verified line range, complete exploit chain.",
                f"- **Leads** ({len(lead_vulns)}) — partial evidence; worth investigating "
                "but not yet a proven consensus bug.",
                "",
            ])

            def _emit_vuln(label: str, diff: dict, idx: int) -> None:
                lines.append(
                    f"#### {label}-{idx}: {diff.get('workflow_id', '?')} / "
                    f"{diff.get('state_id', '?')}"
                )
                lines.append("")
                lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
                deviating = diff.get("deviating_clients", [])
                if deviating:
                    lines.append(f"- **Affected client(s)**: {', '.join(deviating)}")
                lines.append(f"- **Severity**: {diff.get('severity', '?')}")
                eq = diff.get("evidence_quality", "")
                if eq:
                    lines.append(f"- **Evidence quality**: {eq}")
                lines.append(f"- **Production**: {diff.get('is_production', True)} | "
                             f"**Line verified**: {diff.get('line_verified', False)}")
                lines.append(f"- **Description**: {diff.get('description', '')}")
                sec_note = diff.get("security_note", "")
                if sec_note:
                    lines.append(f"- **⚠️ Security Note**: {sec_note}")
                chain = diff.get("exploit_chain") or []
                if chain:
                    lines.append("- **Exploit chain**:")
                    for step in chain:
                        if isinstance(step, dict):
                            lines.append(f"  - {step}")
                        else:
                            lines.append(f"  - {step}")
                dr = diff.get("downgrade_reason", "")
                if dr:
                    lines.append(f"- **Downgrade reason**: {dr}")
                vstatus = diff.get("verification_status", "")
                if vstatus:
                    lines.append(f"- **Verification**: {vstatus}")
                evidence = diff.get("evidence", {})
                if evidence:
                    lines.append("- **Evidence**:")
                    for client, ev in evidence.items():
                        if ev:
                            lines.append(
                                f"  - {client}: `{ev.get('file', '?')}` → "
                                f"`{ev.get('function', '?')}` "
                                f"L{ev.get('lines', [])}"
                                f"{' [TEST]' if ev.get('is_production') is False else ''}"
                            )
                lines.append("")

            if confirmed_vulns:
                lines.extend([f"### ✅ Confirmed Vulnerabilities ({len(confirmed_vulns)})", ""])
                for i, d in enumerate(confirmed_vulns, 1):
                    _emit_vuln("VULN", d, i)

            if lead_vulns:
                lines.extend([f"### 🟡 Leads — Worth Investigating ({len(lead_vulns)})", ""])
                for i, d in enumerate(lead_vulns, 1):
                    _emit_vuln("LEAD", d, i)

        # ── 6b. Cross-Client Structural Logic Differences ──────────────
        lines.extend([
            "## B-Class Structural Logic Differences",
            "",
            "These are **genuine design divergences** between client "
            "implementations that require human review. They reflect "
            "architectural choices, not naming inconsistencies.",
            "",
        ])

        severity_order = [
            ("CRITICAL", "🔴 Critical — Missing Safety Guards / Consensus-Splitting Divergences"),
            ("MAJOR", "🟠 Major — Exploitable Asymmetries (Eclipse, DoS, Chain-Split Vectors)"),
            ("MINOR", "🟡 Minor — Architectural Differences with Limited Attack Surface"),
        ]
        b_idx = 1
        for sev_key, sev_label in severity_order:
            sev_diffs = [d for d in divergence_diffs if d.get("severity") == sev_key]
            if not sev_diffs:
                continue
            lines.extend([
                f"### {sev_label} ({len(sev_diffs)})",
                "",
            ])
            for diff in sev_diffs:
                lines.append(
                    f"#### B-{b_idx}: {diff.get('workflow_id', '?')} / "
                    f"{diff.get('state_id', '?')}"
                )
                lines.append("")
                lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
                deviating = diff.get("deviating_clients", [])
                if deviating:
                    lines.append(
                        f"- **Deviating client(s)**: "
                        f"{', '.join(deviating)}"
                    )
                lines.append(
                    f"- **All clients involved**: "
                    f"{', '.join(diff.get('involved_clients', []))}"
                )
                lines.append(f"- **Description**: {diff.get('description', '')}")
                sec_note = diff.get("security_note", "")
                if sec_note:
                    lines.append(f"- **⚠️ Security Note**: {sec_note}")
                evidence = diff.get("evidence", {})
                if evidence:
                    lines.append("- **Evidence**:")
                    for client, ev in evidence.items():
                        if ev:
                            lines.append(
                                f"  - {client}: `{ev.get('file', '?')}` → "
                                f"`{ev.get('function', '?')}` "
                                f"L{ev.get('lines', [])}"
                            )
                lines.append("")
                b_idx += 1
    else:
        lines.extend([
            "## B-Class Structural Logic Differences",
            "",
            "✅ No structural logic differences found — all clients implement "
            "identical state machines.",
            "",
        ])

    # ── 7. Agreement ───────────────────────────────────────────────────
    lines.extend([
        "## Agreement",
        "",
    ])
    if agreement_wfs:
        lines.append(
            "The following workflows show **complete agreement** across all "
            "clients (no A-class or B-class diffs detected):"
        )
        lines.append("")
        for wf in agreement_wfs:
            lines.append(f"- ✅ `{wf}`")
        lines.append("")
    else:
        lines.append(
            "No workflows show complete agreement across all clients. "
            "Every workflow has at least one A-class or B-class diff."
        )
        lines.append("")

    # ── 8. Iteration Trend ─────────────────────────────────────────────
    if iteration_history:
        lines.extend([
            "## Iteration Trend",
            "",
            "| Workflow | Iter | A-class | B-class | Logic Diff Rate |",
            "|----------|------|---------|---------|-----------------|",
        ])
        # Group iterations by workflow for readability
        current_wf = ""
        for h in iteration_history:
            wf = h.get("workflow", "")
            # Insert a separator row when the workflow changes
            if wf != current_wf:
                if current_wf:
                    lines.append(
                        "| | | | | |"
                    )
                current_wf = wf
            lines.append(
                f"| `{wf}` "
                f"| {h.get('iteration', '?')} "
                f"| {h.get('a_class_count', '?')} "
                f"| {h.get('b_class_count', '?')} "
                f"| {h.get('logic_diff_rate', 0.0):.4f} |"
            )
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(
        "[write_diff_report] → %s (%d A-class, %d B-class diffs)",
        path, len(a_diffs), len(b_diffs),
    )
    return path


def write_diff_report_json(state: dict[str, Any]) -> Path:
    """Write a structured JSON version of the Audit Diff Report.

    Provides the same data as the Markdown report in a machine-readable
    format for downstream tools, dashboards, and CI/CD integration.
    """
    config.OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_PATH / "Audit_Diff_Report.json"

    diff_report = state.get("diff_report", {})
    b_diffs_raw = diff_report.get("b_class_diffs", [])
    a_diffs = diff_report.get("a_class_diffs", [])
    logic_diff_rate = diff_report.get("logic_diff_rate", 0.0)
    total_transitions = diff_report.get("total_transitions", 0)
    force_stopped = state.get("force_stopped", False)
    convergence_reason = state.get("convergence_reason", "")
    iteration_history = state.get("iteration_history", [])

    # Deduplicate and normalize severity
    b_diffs = _deduplicate_b_diffs(b_diffs_raw)
    for d in b_diffs:
        d["severity"] = _normalize_severity(d)

    wf_summary = _per_workflow_summary(a_diffs, b_diffs, total_transitions,
                                       state.get("client_lsgs", {}))
    client_ranking = _per_client_ranking(a_diffs, b_diffs)
    agreement_wfs = _agreement_workflows(a_diffs, b_diffs)
    exec_summary = _generate_executive_summary(
        a_diffs, b_diffs, wf_summary, client_ranking, agreement_wfs, force_stopped,
    )

    sev_counts = Counter(d.get("severity", "MAJOR") for d in b_diffs)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "logic_diff_rate": logic_diff_rate,
        "force_stopped": force_stopped,
        "convergence_reason": convergence_reason,
        "executive_summary": exec_summary,
        "summary": {
            "a_class_count": len(a_diffs),
            "b_class_count": len(b_diffs),
            "b_class_critical": sev_counts.get("CRITICAL", 0),
            "b_class_major": sev_counts.get("MAJOR", 0),
            "b_class_minor": sev_counts.get("MINOR", 0),
            "total_diffs": len(a_diffs) + len(b_diffs),
            "total_transitions": total_transitions,
            "agreement_workflows": len(agreement_wfs),
        },
        "per_workflow_summary": wf_summary,
        "per_client_ranking": client_ranking,
        "agreement_workflows": agreement_wfs,
        "a_class_diffs": a_diffs,
        "b_class_diffs": b_diffs,
        "iteration_history": iteration_history,
    }

    # Add verification summary to JSON if present
    _excluded = _reconcile_verdict_lists(
        {
            "rejected_b_diffs":  state.get("rejected_b_diffs", []),
            "dropped_b_diffs":   state.get("dropped_b_diffs", []),
            "reclassified_to_a": state.get("reclassified_to_a", []),
        },
        kept_b_diffs=b_diffs,
    )
    rejected_diffs     = _excluded["rejected_b_diffs"]
    reclassified_diffs = _excluded["reclassified_to_a"]
    dropped_diffs      = _excluded["dropped_b_diffs"]
    state["rejected_b_diffs"]  = rejected_diffs
    state["dropped_b_diffs"]   = dropped_diffs
    state["reclassified_to_a"] = reclassified_diffs
    evidence_audit = state.get("evidence_audit", [])

    n_confirmed_status = sum(1 for d in b_diffs if d.get("verification_status") == "CONFIRMED")
    n_downgraded       = sum(1 for d in b_diffs if d.get("verification_status") == "DOWNGRADED")
    # Audit-review remediation: 3-tier verdict_class
    n_confirmed = sum(1 for d in b_diffs if d.get("verdict_class") == "confirmed")
    n_lead      = sum(1 for d in b_diffs if d.get("verdict_class") == "lead")
    if (n_confirmed_status + n_downgraded + len(rejected_diffs)
            + len(reclassified_diffs) + len(dropped_diffs)) > 0:
        report["verification_summary"] = {
            "confirmed":    n_confirmed,
            "lead":         n_lead,
            "dropped":      len(dropped_diffs) + len(rejected_diffs),
            "downgraded":   n_downgraded,
            "rejected":     len(rejected_diffs),
            "reclassified": len(reclassified_diffs),
            # Backwards-compatible alias.
            "confirmed_status": n_confirmed_status,
        }
        report["rejected_b_diffs"]   = rejected_diffs
        report["reclassified_to_a"]  = reclassified_diffs
        report["dropped_b_diffs"]    = dropped_diffs
        if evidence_audit:
            report["evidence_audit"] = evidence_audit
        # Headline override.
        report["executive_summary_headline"] = (
            f"{n_confirmed} confirmed / {n_lead} leads / "
            f"{len(dropped_diffs) + len(rejected_diffs)} dropped"
        )

    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    logger.info("[write_diff_report_json] → %s", path)
    return path


def write_false_positives_report(state: dict[str, Any]) -> Path | None:
    """Write rejected (false positive) B-class diffs to a separate file.

    Returns ``None`` if there are no rejected diffs.
    """
    rejected = state.get("rejected_b_diffs", [])
    reclassified = state.get("reclassified_to_a", [])
    dropped = state.get("dropped_b_diffs", [])

    # Defensive reconcile (no-op if already reconciled by write_diff_report).
    diff_report = state.get("diff_report", {})
    kept_b_diffs = _deduplicate_b_diffs(diff_report.get("b_class_diffs", []))
    _excluded = _reconcile_verdict_lists(
        {
            "rejected_b_diffs":  rejected,
            "dropped_b_diffs":   dropped,
            "reclassified_to_a": reclassified,
        },
        kept_b_diffs=kept_b_diffs,
    )
    rejected     = _excluded["rejected_b_diffs"]
    dropped      = _excluded["dropped_b_diffs"]
    reclassified = _excluded["reclassified_to_a"]

    if not rejected and not reclassified and not dropped:
        return None

    config.OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    path = config.OUTPUT_PATH / "Audit_False_Positives.md"

    lines: list[str] = [
        "# EthAuditor — False Positives, Reclassified & Dropped Diffs",
        "",
        f"**Generated at**: {datetime.now(timezone.utc).isoformat()}",
        "",
        "This file contains B-class diffs excluded from the main report:",
        "",
        "- **Rejected** — production code shows the alleged divergence does not exist.",
        "- **Reclassified** — moved to A-class (vocabulary/naming).",
        "- **Dropped (evidence gate)** — evidence cited test/mock code, a non-existent "
        "file, or an out-of-range line range; or `[CONSENSUS VULN]` claim lacked any "
        "production-code path.",
        "",
    ]

    if rejected:
        lines.extend([
            f"## Rejected Diffs ({len(rejected)})",
            "",
            "These diffs were determined to be **false positives** — code "
            "evidence shows the claimed divergence does not exist.",
            "",
        ])
        for i, diff in enumerate(rejected, 1):
            lines.append(f"### FP-{i}: {diff.get('workflow_id', '?')} / "
                         f"{diff.get('state_id', '?')}")
            lines.append("")
            lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
            lines.append(f"- **Original Severity**: {diff.get('severity', '?')}")
            lines.append(f"- **Description**: {diff.get('description', '')}")
            reason = diff.get("rejection_reason", "")
            if reason:
                lines.append(f"- **Rejection Reason**: {reason}")
            lines.append("")

    if reclassified:
        lines.extend([
            f"## Reclassified Diffs ({len(reclassified)})",
            "",
            "These diffs were reclassified from B-class (logic divergence) "
            "to A-class (vocabulary/naming difference). They have been "
            "moved to the A-class section of the main report.",
            "",
        ])
        for i, diff in enumerate(reclassified, 1):
            lines.append(f"### RC-{i}: {diff.get('workflow_id', '?')} / "
                         f"{diff.get('state_id', '?')}")
            lines.append("")
            lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
            lines.append(f"- **Description**: {diff.get('description', '')}")
            reason = diff.get("reclassify_reason", "")
            if reason:
                lines.append(f"- **Reclassify Reason**: {reason}")
            lines.append("")

    if dropped:
        lines.extend([
            f"## Dropped by Evidence Gate ({len(dropped)})",
            "",
            "These findings were silently removed by the deterministic "
            "evidence gate. The cited code is in test / mock / generator "
            "paths, references a non-existent file, has an out-of-range "
            "line number, or is a `[CONSENSUS VULN]` claim with no "
            "supporting production-code path.",
            "",
        ])
        for i, diff in enumerate(dropped, 1):
            lines.append(f"### DROP-{i}: {diff.get('workflow_id', '?')} / "
                         f"{diff.get('state_id', '?')}")
            lines.append("")
            lines.append(f"- **Guard**: `{diff.get('transition_guard', '?')}`")
            lines.append(f"- **Original severity**: {diff.get('severity', '?')}")
            lines.append(f"- **Description**: {diff.get('description', '')}")
            sec = diff.get("security_note", "")
            if sec:
                lines.append(f"- **Security note**: {sec}")
            reason = diff.get("downgrade_reason", "")
            if reason:
                lines.append(f"- **Drop reason**: {reason}")
            evidence = diff.get("evidence", {})
            if evidence:
                lines.append("- **Cited evidence**:")
                for client, ev in evidence.items():
                    if ev:
                        flag = " [TEST]" if ev.get("is_production") is False else ""
                        lines.append(
                            f"  - {client}: `{ev.get('file', '?')}` → "
                            f"`{ev.get('function', '?')}` "
                            f"L{ev.get('lines', [])}{flag}"
                        )
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(
        "[write_false_positives_report] → %s (rejected=%d reclassified=%d dropped=%d)",
        path, len(rejected), len(reclassified), len(dropped),
    )
    return path

