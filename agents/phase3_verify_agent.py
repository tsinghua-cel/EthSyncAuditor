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
    finding: str = "ABSENT"  # PRESENT | ABSENT | PARTIAL | DIFFERENT_LOCATION
    code_evidence: list[CodeEvidence] = Field(default_factory=list)
    explanation: str = ""


class VerifySubResult(BaseModel):
    """Output of a verification sub-agent for one client."""
    client_name: str = ""
    findings: list[VerifySubFinding] = Field(default_factory=list)


class VerifyVerdict(BaseModel):
    """Verdict for a single B-class diff."""
    diff_id: str = ""
    verdict: str = "CONFIRMED"  # CONFIRMED | REJECTED | DOWNGRADED | RECLASSIFIED
    original_severity: str = ""
    new_severity: str = ""
    confidence: float = 0.8
    evidence_summary: str = ""
    updated_description: str = ""
    updated_security_note: str = ""


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

        try:
            from tools.search import search_codebase_by_workflow
            search_available = True
        except ImportError:
            search_available = False

        is_mock = llm is None
        for i, diff in enumerate(b_diffs):
            diff_id = f"B-{i + 1}"
            queries = _build_search_queries(diff)
            code_snippets: list[dict] = []

            if search_available and not is_mock:
                for query in queries:
                    try:
                        results = search_codebase_by_workflow(
                            workflow_id=current_wf,
                            query=query,
                            client_name=client_name,
                            top_k=VERIFY_SEARCH_TOP_K,
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
                return {"verification_evidence": {client_name: findings}}
            except Exception:
                logger.error(
                    "LLM call failed for phase3_verify_sub/%s/%s",
                    client_name, current_wf, exc_info=True,
                )

        # ── Mock fallback: return raw search evidence ──────────────────
        mock_findings: list[dict] = []
        for ev in all_evidence:
            has_code = len(ev.get("code_snippets", [])) > 0
            mock_findings.append({
                "diff_id": ev["diff_id"],
                "finding": "PRESENT" if has_code else "ABSENT",
                "code_evidence": ev.get("code_snippets", [])[:3],
                "explanation": (
                    f"Found {len(ev.get('code_snippets', []))} code snippets "
                    f"for guard '{ev['diff_guard']}' in {client_name}"
                    if has_code else
                    f"No code evidence found for guard '{ev['diff_guard']}' "
                    f"in {client_name}"
                ),
            })
        return {"verification_evidence": {client_name: mock_findings}}

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
    1. If ALL deviating clients have PRESENT finding → REJECTED
       (the feature exists, just named/located differently).
    2. If some deviating clients have DIFFERENT_LOCATION and none ABSENT
       → RECLASSIFIED (naming/structural difference, not logic).
    3. If some deviating clients have PARTIAL and none ABSENT
       → DOWNGRADED (partially implemented → lower severity).
    4. Otherwise (ABSENT in ≥1 deviating client) → CONFIRMED.
    """
    verified: list[dict] = []
    rejected: list[dict] = []
    reclassified: list[dict] = []

    # Index findings by (client, diff_id)
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
            verified.append({**diff, "verification_status": "CONFIRMED"})
            continue

        # Gather findings for each deviating client
        dev_findings: list[str] = []
        for client in deviating:
            f = findings_idx.get((client, diff_id), {})
            dev_findings.append(f.get("finding", "ABSENT"))

        present_count = sum(1 for f in dev_findings if f == "PRESENT")
        partial_count = sum(1 for f in dev_findings if f == "PARTIAL")
        diff_loc_count = sum(1 for f in dev_findings if f == "DIFFERENT_LOCATION")
        absent_count = sum(1 for f in dev_findings if f == "ABSENT")

        if present_count > 0 and absent_count == 0:
            rejected.append({
                **diff,
                "verification_status": "REJECTED",
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
                "diff_type": "A",
                "reclassify_reason": (
                    f"Feature exists in deviating client(s) but in a "
                    f"different module/location — naming/structural "
                    f"difference, not a logic divergence."
                ),
            })
        elif partial_count > 0 and absent_count == 0:
            new_sev = "MINOR" if severity == "MAJOR" else (
                "MAJOR" if severity == "CRITICAL" else severity
            )
            verified.append({
                **diff,
                "verification_status": "DOWNGRADED",
                "original_severity": severity,
                "severity": new_sev,
            })
        else:
            verified.append({**diff, "verification_status": "CONFIRMED"})

    logger.info(
        "[phase3_verify] wf=%s — verified=%d rejected=%d reclassified=%d",
        current_wf, len(verified), len(rejected), len(reclassified),
    )

    return {
        "verified_b_diffs": verified,
        "rejected_b_diffs": rejected,
        "reclassified_to_a": reclassified,
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

    for i, diff in enumerate(b_diffs):
        diff_id = f"B-{i + 1}"
        v = verdict_map.get(diff_id)

        if v is None:
            verified.append({**diff, "verification_status": "CONFIRMED"})
            continue

        if v.verdict == "REJECTED":
            rejected.append({
                **diff,
                "verification_status": "REJECTED",
                "rejection_reason": v.evidence_summary,
                "verification_confidence": v.confidence,
            })
        elif v.verdict == "RECLASSIFIED":
            reclassified.append({
                **diff,
                "verification_status": "RECLASSIFIED",
                "diff_type": "A",
                "reclassify_reason": v.evidence_summary,
                "verification_confidence": v.confidence,
                "description": v.updated_description or diff.get("description", ""),
            })
        elif v.verdict == "DOWNGRADED":
            verified.append({
                **diff,
                "verification_status": "DOWNGRADED",
                "original_severity": diff.get("severity", ""),
                "severity": v.new_severity or diff.get("severity", ""),
                "description": v.updated_description or diff.get("description", ""),
                "security_note": v.updated_security_note or diff.get("security_note", ""),
                "verification_confidence": v.confidence,
            })
        else:  # CONFIRMED
            updated = {**diff, "verification_status": "CONFIRMED"}
            if v.updated_description:
                updated["description"] = v.updated_description
            if v.updated_security_note:
                updated["security_note"] = v.updated_security_note
            if v.evidence_summary:
                updated["evidence_summary"] = v.evidence_summary
            updated["verification_confidence"] = v.confidence
            verified.append(updated)

    logger.info(
        "[phase3_verify] wf=%s — verified=%d rejected=%d reclassified=%d",
        current_wf, len(verified), len(rejected), len(reclassified),
    )

    return {
        "verified_b_diffs": verified,
        "rejected_b_diffs": rejected,
        "reclassified_to_a": reclassified,
    }

