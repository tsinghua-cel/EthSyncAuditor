"""Tests for Phase 2 sub-agent evidence grounding.

Verifies that _ground_workflow clears hallucinated file paths and keeps
evidence that resolves to a real retrieved snippet.
"""

from __future__ import annotations

from agents.phase2_sub_agent import _ground_workflow, _retrieve_code_context


def _snip(n: int, file: str, fn: str = "f") -> dict:
    return {
        "id": f"prysm::initial_sync::S{n}",
        "file": file,
        "function": fn,
        "start_line": 10,
        "end_line": 50,
        "code": "code",
    }


def _tr(guard: str, file: str | None, function: str = "f") -> dict:
    ev = {"file": file, "function": function, "lines": [10, 20]} if file else None
    return {"guard": guard, "actions": [], "next_state": "done", "evidence": ev}


def _wf(transitions: list[dict]) -> dict:
    return {
        "id": "initial_sync",
        "states": [
            {"id": "s1", "label": "S1", "category": "init", "transitions": transitions}
        ],
    }


def test_real_file_evidence_kept():
    snippets = [_snip(1, "beacon-chain/sync/batch.go")]
    wf = _wf([_tr("G1", "beacon-chain/sync/batch.go")])
    out, kept, cleared = _ground_workflow(wf, snippets, "prysm")
    assert kept == 1 and cleared == 0
    assert out["states"][0]["transitions"][0]["evidence"]["file"] == "beacon-chain/sync/batch.go"


def test_hallucinated_file_cleared():
    snippets = [_snip(1, "beacon-chain/sync/batch.go")]
    wf = _wf([_tr("G1", "some/invented/path.go")])
    out, kept, cleared = _ground_workflow(wf, snippets, "prysm")
    assert kept == 0 and cleared == 1
    assert out["states"][0]["transitions"][0]["evidence"] is None


def test_null_evidence_unchanged():
    snippets = [_snip(1, "beacon-chain/sync/batch.go")]
    wf = _wf([_tr("G1", None)])  # already null
    out, kept, cleared = _ground_workflow(wf, snippets, "prysm")
    assert kept == 0 and cleared == 0
    assert out["states"][0]["transitions"][0]["evidence"] is None


def test_basename_match_kept():
    """A transition citing only the basename of a retrieved file is accepted."""
    snippets = [_snip(1, "beacon-chain/sync/batch.go")]
    wf = _wf([_tr("G1", "batch.go")])  # basename only
    out, kept, cleared = _ground_workflow(wf, snippets, "prysm")
    assert kept == 1 and cleared == 0


def test_mixed_transitions():
    snippets = [_snip(1, "beacon-chain/sync/batch.go"),
                _snip(2, "beacon-chain/sync/service.go")]
    wf = _wf([
        _tr("G1", "beacon-chain/sync/batch.go"),    # real → kept
        _tr("G2", "nonexistent/file.go"),            # fake → cleared
        _tr("G3", None),                             # null → unchanged
        _tr("G4", "beacon-chain/sync/service.go"),  # real → kept
    ])
    out, kept, cleared = _ground_workflow(wf, snippets, "prysm")
    assert kept == 2 and cleared == 1
    transitions = out["states"][0]["transitions"]
    assert transitions[0]["evidence"] is not None   # G1 kept
    assert transitions[1]["evidence"] is None        # G2 cleared
    assert transitions[2]["evidence"] is None        # G3 null
    assert transitions[3]["evidence"] is not None   # G4 kept


def test_retrieval_returns_more_snippets_than_before():
    """New multi-query retrieval should return more snippets than the old
    single-query approach (which often returned only 5 or fewer results)."""
    snippets = _retrieve_code_context("prysm", "initial_sync", 1, None)
    # Should get significantly more than the old top_k=10 single query
    assert len(snippets) >= 10, f"expected ≥10 snippets, got {len(snippets)}"
    # All returned snippets must have stable IDs
    for s in snippets:
        assert s["id"].startswith("prysm::initial_sync::S")
        assert s["file"]


def test_retrieval_all_files_have_ids():
    snippets = _retrieve_code_context("lighthouse", "regular_sync", 1, None)
    ids = [s["id"] for s in snippets]
    assert len(ids) == len(set(ids)), "duplicate evidence IDs"
    for s in snippets:
        assert s["start_line"] >= 0
