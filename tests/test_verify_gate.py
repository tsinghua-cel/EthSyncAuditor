"""Tests for the Phase 3 verification evidence gate.

These lock in the behaviour that a B-class diff only reaches the main report
(``verified_b_diffs``) when a CONFIRMED/DOWNGRADED verdict cites real,
preprocessing-backed code evidence. Everything else is routed to
``unverified_b_diffs`` (not silently confirmed, and not mislabelled as a
false positive).
"""

from __future__ import annotations

from agents.phase3_verify_agent import (
    VerifyVerdict,
    _apply_verdicts,
    _deterministic_verify,
)


def _diff(i: int = 1) -> dict:
    return {
        "workflow_id": "initial_sync",
        "state_id": f"initial_sync.s{i}",
        "transition_guard": "SomeGuard",
        "description": "desc",
        "severity": "CRITICAL",
        "deviating_clients": ["prysm"],
        "involved_clients": ["prysm", "teku"],
    }


def _evidence_map(diff_id: str = "B-1", with_code: bool = True) -> dict:
    code = (
        [{
            "id": f"prysm::{diff_id}::1",
            "file": "beacon-chain/sync/x.go",
            "function": "DoThing",
            "lines": [10, 20],
            "snippet": "code",
        }]
        if with_code else []
    )
    return {
        "prysm": [{
            "diff_id": diff_id,
            "finding": "ABSENT",
            "explanation": "no guard found",
            "code_evidence": code,
        }],
    }


# ── _apply_verdicts (LLM path) ────────────────────────────────────────────


def test_missing_verdict_is_unverified_not_confirmed():
    out = _apply_verdicts([_diff()], [], _evidence_map(), "initial_sync")
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1
    assert out["unverified_b_diffs"][0]["verification_status"] == "INSUFFICIENT_EVIDENCE"


def test_confirmed_without_cited_evidence_is_demoted():
    v = VerifyVerdict(diff_id="B-1", verdict="CONFIRMED", cited_evidence_ids=[])
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1


def test_confirmed_with_hallucinated_id_is_demoted():
    v = VerifyVerdict(
        diff_id="B-1", verdict="CONFIRMED",
        cited_evidence_ids=["prysm::B-1::999"],  # not in the real pool
    )
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1


def test_confirmed_with_valid_id_is_verified_with_evidence():
    v = VerifyVerdict(
        diff_id="B-1", verdict="CONFIRMED",
        cited_evidence_ids=["prysm::B-1::1"],
    )
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert len(out["verified_b_diffs"]) == 1
    ev = out["verified_b_diffs"][0]["evidence"]
    assert ev["prysm"]["file"] == "beacon-chain/sync/x.go"
    assert ev["prysm"]["lines"] == [10, 20]


def test_insufficient_evidence_verdict_routes_to_unverified():
    v = VerifyVerdict(diff_id="B-1", verdict="INSUFFICIENT_EVIDENCE")
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1


def test_rejected_routes_to_rejected():
    v = VerifyVerdict(diff_id="B-1", verdict="REJECTED", evidence_summary="exists")
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert len(out["rejected_b_diffs"]) == 1
    assert out["verified_b_diffs"] == []


def test_downgraded_with_valid_id_sets_new_severity():
    v = VerifyVerdict(
        diff_id="B-1", verdict="DOWNGRADED", new_severity="MINOR",
        cited_evidence_ids=["prysm::B-1::1"],
    )
    out = _apply_verdicts([_diff()], [v], _evidence_map(), "initial_sync")
    assert len(out["verified_b_diffs"]) == 1
    assert out["verified_b_diffs"][0]["severity"] == "MINOR"
    assert out["verified_b_diffs"][0]["original_severity"] == "CRITICAL"


# ── _deterministic_verify (mock / fallback path) ──────────────────────────


def test_deterministic_absent_without_evidence_is_unverified():
    out = _deterministic_verify(
        [_diff()], _evidence_map(with_code=False), "initial_sync",
    )
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1


def test_deterministic_absent_with_evidence_is_confirmed():
    out = _deterministic_verify([_diff()], _evidence_map(), "initial_sync")
    assert len(out["verified_b_diffs"]) == 1
    assert out["verified_b_diffs"][0]["verification_status"] == "CONFIRMED"


def test_deterministic_present_is_rejected():
    em = _evidence_map()
    em["prysm"][0]["finding"] = "PRESENT"
    out = _deterministic_verify([_diff()], em, "initial_sync")
    assert len(out["rejected_b_diffs"]) == 1
    assert out["verified_b_diffs"] == []


def test_deterministic_no_deviating_clients_is_unverified():
    d = _diff()
    d["deviating_clients"] = []
    out = _deterministic_verify([d], {}, "initial_sync")
    assert out["verified_b_diffs"] == []
    assert len(out["unverified_b_diffs"]) == 1
