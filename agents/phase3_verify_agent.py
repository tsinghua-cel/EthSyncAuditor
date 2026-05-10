"""Phase 3 Verification Agent.

After each workflow's Phase 2 B-class discovery converges, this module
verifies whether the reported B-class diffs are genuine by searching the
source code of deviating clients.

Two components:
  - **Verify Sub-Agent**: searches one client's codebase for evidence
    supporting or refuting a set of B-class diffs.
  - **Verify Main Agent**: aggregates evidence from all sub-agents and
    issues a verdict for each diff (CONFIRMED / REJECTED / DOWNGRADED /
    RECLASSIFIED).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Template
from pydantic import BaseModel, Field

from config import CLIENT_NAMES, VERIFY_SEARCH_TOP_K
from utils import invoke_with_retry

logger = logging.getLogger(__name__)

_SUB_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase3_verify_sub.j2"
_MAIN_PROMPT_PATH = Path(__file__).parent / "prompts" / "phase3_verify_main.j2"


def _load_template(path: Path) -> Template:
    return Template(path.read_text(encoding="utf-8"))


# ── Pydantic schemas for structured LLM output ─────────────────────────


class CodeEvidence(BaseModel):
    """A single code snippet found during verification."""
    file: str = ""
    function: str = ""
    lines: list[int] = Field(default_factory=list)
    snippet: str = ""
    relevance: str = ""


class VerifySubFinding(BaseModel):
    """One sub-agent's finding about a single B-class diff."""
    diff_id: str = ""
    finding: str = "ABSENT"  # PRESENT | ABSENT | PARTIAL | DIFFERENT_LOCATION | EVIDENCE_INVALID
    code_evidence: list[CodeEvidence] = Field(default_factory=list)
    explanation: str = ""
    # ── Audit-review remediation ────────────────────────────────────────
    evidence_quality: str = "STRONG"     # STRONG | WEAK | INVALID
    production_path: str | None = None
    line_range_verified: bool = True


class VerifySubResult(BaseModel):
    """Output of a verification sub-agent for one client."""
    client_name: str = ""
    findings: list[VerifySubFinding] = Field(default_factory=list)


class VerifyVerdict(BaseModel):
    """Verdict for a single B-class diff."""
    diff_id: str = ""
    verdict: str = "CONFIRMED"  # CONFIRMED | REJECTED | DOWNGRADED | RECLASSIFIED | DROPPED
    original_severity: str = ""
    new_severity: str = ""
    confidence: float = 0.8
    evidence_summary: str = ""
    updated_description: str = ""
    updated_security_note: str = ""
    # ── Audit-review remediation ────────────────────────────────────────
    is_production: bool = True
    line_verified: bool = True
    exploit_chain_present: bool = False
    downgrade_reason: str = ""


class VerifyMainResult(BaseModel):
    """Output of the verification main agent for one workflow."""
    workflow_id: str = ""
    verdicts: list[VerifyVerdict] = Field(default_factory=list)


# Search query extraction


def _extract_identifiers_from_text(text: str) -> list[str]:
    """Extract PascalCase/camelCase/snake_case identifiers from free text.

    Finds tokens like 'HandleInvalidPayloadStatus', 'InvalidateDescendants',
    'engine_forkchoice_updated', etc. that are likely code-level names.
    """
    import re
    # Match identifiers with ≥2 sub-words (PascalCase, camelCase, or snake_case)
    pascal_camel = re.findall(r"\b[A-Z][a-zA-Z0-9]{5,}\b", text)
    snake = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b", text)
    # Also match quoted identifiers like 'FooBar' or `FooBar`
    quoted = re.findall(r"['\"`]([A-Za-z_][A-Za-z0-9_]{4,})['\"`]", text)
    return pascal_camel + snake + quoted


def _build_search_queries(diff: dict) -> list[str]:
    """Extract search queries from a B-class diff for code search.

    Enhanced version that also mines the description and security_note
    for code-level identifiers (function names, guard names, action names).
    """
    queries: list[str] = []

    guard = diff.get("transition_guard", "")
    if guard and guard not in ("*", "TRUE"):
        queries.append(guard)

    desc = diff.get("description", "")
    sec_note = diff.get("security_note", "")
    combined_text = f"{desc} {sec_note}"

    # ── Extract code-level identifiers from description & security_note ──
    identifiers = _extract_identifiers_from_text(combined_text)
    for ident in identifiers:
        if ident not in queries:
            queries.append(ident)

    # ── Domain-specific keyword matching ─────────────────────────────────
    for term in [
        "backfill", "optimistic", "checkpoint", "slashing",
        "fork choice", "forkchoice", "circuit breaker",
        "peer penalty", "penalize", "peer score",
        "blob", "kzg", "commitment", "subnet",
        "payload", "new_payload", "forkchoice_updated",
        "reorg", "reorganize", "rollback",
        "builder", "mev", "external signer",
        "equivocation", "equivocating",
        "attestation", "aggregate", "sync committee",
        "transition configuration", "exchange capabilities",
        "halt", "disconnect", "reconnect",
        "selection proof", "aggregator",
        # Consensus vulnerability terms
        "race condition", "mutex", "lock", "rwlock", "atomic",
        "validate", "verify_signature", "bls_verify",
        "overflow", "underflow", "saturating",
        "rate limit", "throttle", "max_size", "bounded",
        "latest_valid_hash", "invalid_block_root",
        "is_valid_indexed_attestation", "process_block",
        "state_transition", "get_head", "compute_domain",
        "slashing_protection", "signing_root",
    ]:
        if term.lower() in combined_text.lower():
            queries.append(term)

    state_id = diff.get("state_id", "")
    if state_id and not state_id.endswith(".*"):
        cat = state_id.rsplit(".", 1)[-1] if "." in state_id else state_id
        if cat not in ("init", "done", "terminal", "*"):
            queries.append(cat)

    wf_id = diff.get("workflow_id", "")
    if wf_id and guard and guard not in ("*", "TRUE"):
        queries.append(f"{wf_id} {guard}")

    # ── Also search for action names mentioned in the diff ───────────────
    for action in diff.get("actions", []):
        if action and action not in queries:
            queries.append(action)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        ql = q.lower()
        if ql not in seen:
            seen.add(ql)
            unique.append(q)
    return unique[:8]  # Increased from 5 to 8 for better coverage


# Verify Sub-Agent


def build_phase3_verify_sub_agent(client_name: str, llm=None, callbacks=None):
    """Build a Phase 3 Verification Sub-Agent for *client_name*.

    Searches the client's codebase for evidence supporting or refuting
    the B-class diffs assigned to it.
    """

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        current_wf = state.get("current_workflow", "")
        b_diffs = state.get("_verify_b_diffs", [])

        if not b_diffs:
            logger.info(
                "[phase3_verify_sub] client=%s wf=%s — no diffs to verify",
                client_name, current_wf,
            )
            return {"verification_evidence": {client_name: []}}

        logger.info(
            "[phase3_verify_sub] client=%s wf=%s — verifying %d diffs",
            client_name, current_wf, len(b_diffs),
        )

        # ── Code search phase ──────────────────────────────────────────
        all_evidence: list[dict] = []
        prefilter_findings: dict[str, dict] = {}
        evidence_audit_records: list[dict] = []

        try:
            from tools.search import search_codebase_by_workflow
            search_available = True
        except ImportError:
            search_available = False

        # Lazy import — kept inside _run to avoid hard dep at module load.
        from tools.evidence_validator import (
            DROP, validate_evidence,
        )
        from tools.source_reader import read_source_lines

        is_mock = llm is None
        for i, diff in enumerate(b_diffs):
            diff_id = f"B-{i + 1}"

            # ── (1) Hard pre-filter on the LLM-supplied evidence ────────
            llm_evidence = (diff.get("evidence") or {}).get(client_name)
            if hasattr(llm_evidence, "model_dump"):
                llm_evidence = llm_evidence.model_dump()
            ev_verdict = validate_evidence(llm_evidence, client_name)
            evidence_audit_records.append({
                "workflow_id": current_wf,
                "diff_id": diff_id,
                "client": client_name,
                "stage": "phase3_sub_prefilter",
                **ev_verdict.to_dict(),
            })

            # ── (2) Build prompt-side snippets: prefer the cited code,
            #        fall back to call-graph search (production-only). ──
            code_snippets: list[dict] = []
            if ev_verdict.action != DROP and ev_verdict.excerpt:
                code_snippets.append({
                    "file": llm_evidence.get("file", "") if llm_evidence else "",
                    "function": llm_evidence.get("function", "") if llm_evidence else "",
                    "lines": [ev_verdict.total_lines and 0, ev_verdict.total_lines and 0],
                    "snippet": ev_verdict.excerpt[:1500],
                    "query": "<cited-evidence>",
                    "score": 1.0,
                    "is_production": ev_verdict.is_production,
                    "test_reason": ev_verdict.test_reason,
                })

            if search_available and not is_mock:
                queries = _build_search_queries(diff)
                for query in queries:
                    try:
                        # Production-only by default — filters Rust cfg(test),
                        # *_test.go, *.test.ts, /tests/, mocks, etc.
                        results = search_codebase_by_workflow(
                            workflow_id=current_wf,
                            query=query,
                            client_name=client_name,
                            top_k=VERIFY_SEARCH_TOP_K,
                            production_only=True,
                        )
                        for r in results:
                            code_snippets.append({
                                "file": r.metadata.get("file_path", ""),
                                "function": r.metadata.get("function_name", ""),
                                "lines": [
                                    r.metadata.get("start_line", 0),
                                    r.metadata.get("end_line", 0),
                                ],
                                "snippet": r.content[:500] if r.content else "",
                                "query": query,
                                "score": r.score,
                                "is_production": r.metadata.get("is_production", True),
                                "test_reason": r.metadata.get("test_reason", ""),
                            })
                    except Exception:
                        logger.debug(
                            "[phase3_verify_sub] search failed q=%s",
                            query, exc_info=True,
                        )

            all_evidence.append({
                "diff_id": diff_id,
                "diff_guard": diff.get("transition_guard", ""),
                "diff_state_id": diff.get("state_id", ""),
                "client": client_name,
                "code_snippets": code_snippets,
            })

            # ── (3) If pre-filter said DROP and we ALSO got no production
            #        snippets from search, short-circuit the LLM call. ──
            has_prod_snippet = any(s.get("is_production", True)
                                   for s in code_snippets)
            if ev_verdict.action == DROP and not has_prod_snippet:
                prefilter_findings[diff_id] = {
                    "diff_id": diff_id,
                    "finding": "EVIDENCE_INVALID",
                    "code_evidence": [],
                    "explanation": (
                        f"Pre-filter dropped: {ev_verdict.downgrade_reason}. "
                        f"No production-code snippets found by directed search."
                    ),
                    "evidence_quality": "INVALID",
                    "production_path": None,
                    "line_range_verified": ev_verdict.line_verified,
                }

        # ── LLM analysis (if available) ────────────────────────────────
        if llm is not None:
            template = _load_template(_SUB_PROMPT_PATH)
            prompt = template.render(
                client_name=client_name,
                workflow_id=current_wf,
                b_diffs=b_diffs,
                evidence_per_diff=all_evidence,
            )
            try:
                chain = llm.with_structured_output(VerifySubResult)
                result: VerifySubResult = invoke_with_retry(
                    chain, prompt,
                    label=f"phase3_verify_sub/{client_name}/{current_wf}",
                    callbacks=callbacks,
                )
                findings = [f.model_dump() for f in result.findings]
                # Pre-filter wins over LLM for deterministic INVALID cases.
                merged: list[dict] = []
                seen: set[str] = set()
                for f in findings:
                    fid = f.get("diff_id", "")
                    if fid in prefilter_findings:
                        merged.append(prefilter_findings[fid])
                    else:
                        merged.append(f)
                    seen.add(fid)
                for fid, pf in prefilter_findings.items():
                    if fid not in seen:
                        merged.append(pf)
                return {
                    "verification_evidence": {client_name: merged},
                    "evidence_audit": evidence_audit_records,
                }
            except Exception:
                logger.error(
                    "LLM call failed for phase3_verify_sub/%s/%s",
                    client_name, current_wf, exc_info=True,
                )

        # ── Mock fallback: return raw search evidence ──────────────────
        mock_findings: list[dict] = []
        for ev in all_evidence:
            fid = ev["diff_id"]
            if fid in prefilter_findings:
                mock_findings.append(prefilter_findings[fid])
                continue
            prod_snippets = [s for s in ev.get("code_snippets", [])
                             if s.get("is_production", True)]
            has_code = len(prod_snippets) > 0
            mock_findings.append({
                "diff_id": fid,
                "finding": "PRESENT" if has_code else "ABSENT",
                "code_evidence": prod_snippets[:3],
                "explanation": (
                    f"Found {len(prod_snippets)} production snippets "
                    f"for guard '{ev['diff_guard']}' in {client_name}"
                    if has_code else
                    f"No production code evidence for guard "
                    f"'{ev['diff_guard']}' in {client_name}"
                ),
                "evidence_quality": "STRONG" if has_code else "INVALID",
                "production_path": (
                    prod_snippets[0]["file"] if prod_snippets else None
                ),
                "line_range_verified": has_code,
            })
        return {
            "verification_evidence": {client_name: mock_findings},
            "evidence_audit": evidence_audit_records,
        }

    return _run


# Verify Main Agent


def build_phase3_verify_main_agent(llm=None, callbacks=None):
    """Build the Phase 3 Verification Main Agent.

    Aggregates evidence from all sub-agents and issues a verdict for each
    B-class diff: CONFIRMED, REJECTED, DOWNGRADED, or RECLASSIFIED.
    """

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        current_wf = state.get("current_workflow", "")
        b_diffs = state.get("_verify_b_diffs", [])
        evidence_map = state.get("verification_evidence", {})

        if not b_diffs:
            logger.info("[phase3_verify_main] wf=%s — no diffs to verify", current_wf)
            return {}

        logger.info(
            "[phase3_verify_main] wf=%s — judging %d diffs with evidence "
            "from %d clients",
            current_wf, len(b_diffs), len(evidence_map),
        )

        # ── LLM path ──────────────────────────────────────────────────
        if llm is not None:
            template = _load_template(_MAIN_PROMPT_PATH)
            prompt = template.render(
                workflow_id=current_wf,
                b_diffs=b_diffs,
                evidence_map=evidence_map,
                client_names=CLIENT_NAMES,
            )
            try:
                chain = llm.with_structured_output(VerifyMainResult)
                result: VerifyMainResult = invoke_with_retry(
                    chain, prompt,
                    label=f"phase3_verify_main/{current_wf}",
                    callbacks=callbacks,
                )
                return _apply_verdicts(b_diffs, result.verdicts, current_wf)
            except Exception:
                logger.error(
                    "LLM call failed for phase3_verify_main/%s",
                    current_wf, exc_info=True,
                )

        # ── Deterministic heuristic fallback ───────────────────────────
        return _deterministic_verify(b_diffs, evidence_map, current_wf)

    return _run


# Deterministic verification heuristic (mock / fallback)


def _deterministic_verify(
    b_diffs: list[dict],
    evidence_map: dict[str, list],
    current_wf: str,
) -> dict[str, Any]:
    """Heuristic verification when no LLM is available.

    Rules:
    1. If ANY deviating client has EVIDENCE_INVALID → DROPPED.
    2. If ALL deviating clients have PRESENT finding → REJECTED.
    3. If some deviating clients have DIFFERENT_LOCATION and none ABSENT
       → RECLASSIFIED.
    4. If some deviating clients have PARTIAL and none ABSENT
       → DOWNGRADED.
    5. Otherwise → CONFIRMED.
    """
    verified: list[dict] = []
    rejected: list[dict] = []
    reclassified: list[dict] = []
    dropped: list[dict] = []

    findings_idx: dict[tuple[str, str], dict] = {}
    for client, findings in evidence_map.items():
        if not isinstance(findings, list):
            continue
        for f in findings:
            findings_idx[(client, f.get("diff_id", ""))] = f

    for i, diff in enumerate(b_diffs):
        diff_id = f"B-{i + 1}"
        deviating = diff.get("deviating_clients", [])
        severity = diff.get("severity", "MAJOR")

        if not deviating:
            verified.append({**diff, "verification_status": "CONFIRMED",
                             "verdict_class": "confirmed"})
            continue

        dev_findings: list[str] = []
        for client in deviating:
            f = findings_idx.get((client, diff_id), {})
            dev_findings.append(f.get("finding", "ABSENT"))

        invalid_count   = sum(1 for f in dev_findings if f == "EVIDENCE_INVALID")
        present_count   = sum(1 for f in dev_findings if f == "PRESENT")
        partial_count   = sum(1 for f in dev_findings if f == "PARTIAL")
        diff_loc_count  = sum(1 for f in dev_findings if f == "DIFFERENT_LOCATION")
        absent_count    = sum(1 for f in dev_findings if f == "ABSENT")

        if invalid_count >= 1:
            dropped.append({
                **diff,
                "verification_status": "DROPPED",
                "verdict_class": "dropped",
                "evidence_quality": "INVALID",
                "downgrade_reason": (
                    "Evidence invalid for at least one deviating client "
                    "(test code / non-existent file / out-of-range lines)."
                ),
            })
        elif present_count > 0 and absent_count == 0:
            rejected.append({
                **diff,
                "verification_status": "REJECTED",
                "verdict_class": "dropped",
                "rejection_reason": (
                    f"Code evidence shows the feature described by guard "
                    f"'{diff.get('transition_guard', '?')}' is PRESENT in "
                    f"all deviating clients ({', '.join(deviating)})."
                ),
            })
        elif diff_loc_count > 0 and absent_count == 0:
            reclassified.append({
                **diff,
                "verification_status": "RECLASSIFIED",
                "verdict_class": "lead",
                "diff_type": "A",
                "reclassify_reason": (
                    "Feature exists in deviating client(s) but in a different "
                    "module/location — naming/structural difference."
                ),
            })
        elif partial_count > 0 and absent_count == 0:
            new_sev = "MINOR" if severity == "MAJOR" else (
                "MAJOR" if severity == "CRITICAL" else severity
            )
            verified.append({
                **diff,
                "verification_status": "DOWNGRADED",
                "verdict_class": "lead",
                "evidence_quality": "WEAK",
                "original_severity": severity,
                "severity": new_sev,
            })
        else:
            verified.append({**diff, "verification_status": "CONFIRMED",
                             "verdict_class": "confirmed"})

    logger.info(
        "[phase3_verify] wf=%s — confirmed/lead=%d rejected=%d reclassified=%d dropped=%d",
        current_wf, len(verified), len(rejected), len(reclassified), len(dropped),
    )

    return {
        "verified_b_diffs": verified,
        "rejected_b_diffs": rejected,
        "reclassified_to_a": reclassified,
        "dropped_b_diffs": dropped,
    }


def _apply_verdicts(
    b_diffs: list[dict],
    verdicts: list[VerifyVerdict],
    current_wf: str,
) -> dict[str, Any]:
    """Apply LLM-generated verdicts to the B-class diffs."""
    verdict_map: dict[str, VerifyVerdict] = {v.diff_id: v for v in verdicts}

    verified: list[dict] = []
    rejected: list[dict] = []
    reclassified: list[dict] = []
    dropped: list[dict] = []

    for i, diff in enumerate(b_diffs):
        diff_id = f"B-{i + 1}"
        v = verdict_map.get(diff_id)

        if v is None:
            verified.append({**diff, "verification_status": "CONFIRMED",
                             "verdict_class": "confirmed"})
            continue

        common = {
            "verification_confidence": v.confidence,
            "is_production": v.is_production,
            "line_verified": v.line_verified,
            "exploit_chain_present": v.exploit_chain_present,
        }

        if v.verdict == "DROPPED":
            dropped.append({
                **diff,
                "verification_status": "DROPPED",
                "verdict_class": "dropped",
                "evidence_quality": "INVALID",
                "downgrade_reason": v.downgrade_reason or v.evidence_summary,
                **common,
            })
        elif v.verdict == "REJECTED":
            rejected.append({
                **diff,
                "verification_status": "REJECTED",
                "verdict_class": "dropped",
                "rejection_reason": v.evidence_summary,
                **common,
            })
        elif v.verdict == "RECLASSIFIED":
            reclassified.append({
                **diff,
                "verification_status": "RECLASSIFIED",
                "verdict_class": "lead",
                "diff_type": "A",
                "reclassify_reason": v.evidence_summary,
                "description": v.updated_description or diff.get("description", ""),
                **common,
            })
        elif v.verdict == "DOWNGRADED":
            verified.append({
                **diff,
                "verification_status": "DOWNGRADED",
                "verdict_class": "lead",
                "evidence_quality": "WEAK",
                "original_severity": diff.get("severity", ""),
                "severity": v.new_severity or diff.get("severity", ""),
                "description": v.updated_description or diff.get("description", ""),
                "security_note": v.updated_security_note or diff.get("security_note", ""),
                "downgrade_reason": v.downgrade_reason,
                **common,
            })
        else:  # CONFIRMED
            updated = {
                **diff,
                "verification_status": "CONFIRMED",
                "verdict_class": (
                    "confirmed" if v.exploit_chain_present and v.is_production
                    and v.line_verified else "lead"
                ),
                "evidence_quality": "STRONG" if v.exploit_chain_present else "WEAK",
                **common,
            }
            if v.updated_description:
                updated["description"] = v.updated_description
            if v.updated_security_note:
                updated["security_note"] = v.updated_security_note
            if v.evidence_summary:
                updated["evidence_summary"] = v.evidence_summary
            verified.append(updated)

    logger.info(
        "[phase3_verify] wf=%s — confirmed/lead=%d rejected=%d reclassified=%d dropped=%d",
        current_wf, len(verified), len(rejected), len(reclassified), len(dropped),
    )

    return {
        "verified_b_diffs": verified,
        "rejected_b_diffs": rejected,
        "reclassified_to_a": reclassified,
        "dropped_b_diffs": dropped,
    }

