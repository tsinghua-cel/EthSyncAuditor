"""Tests for Phase 1 grounded vocabulary discovery.

Verifies the plan -> local-retrieve -> extract pipeline, and that the evidence
gate drops entries whose evidence does not resolve to a real retrieved file.
"""

from __future__ import annotations

import pytest

from agents.phase1_sub_agent import SearchPlan, SearchQuery, build_phase1_sub_agent
from state import VocabDiscoveryReport, VocabEntry
from tools.search import SearchResult


class _FakeChain:
    def __init__(self, obj):
        self._obj = obj

    def invoke(self, prompt, **kwargs):
        return self._obj


class _FakeLLM:
    """Returns a SearchPlan for the planning call and a VocabDiscoveryReport
    for the extraction call, dispatching on the requested schema."""

    def __init__(self, plan, report):
        self._plan = plan
        self._report = report

    def with_structured_output(self, schema, **kwargs):
        if schema is SearchPlan:
            return _FakeChain(self._plan)
        return _FakeChain(self._report)


def _fake_result():
    return SearchResult(
        content="if blobsAvailable { ... }",
        metadata={
            "file_path": "beacon-chain/sync/x.go",
            "function_name": "DoIt",
            "start_line": 1,
            "end_line": 9,
        },
        score=1.0,
    )


def test_grounded_extraction_drops_ungrounded_entries(monkeypatch):
    monkeypatch.setattr(
        "tools.search.search_codebase_by_workflow",
        lambda **kw: [_fake_result()],
    )
    monkeypatch.setattr("tools.search.search_codebase", lambda *a, **k: [_fake_result()])

    plan = SearchPlan(queries=[
        SearchQuery(workflow_id="initial_sync", query="blob availability"),
    ])
    report = VocabDiscoveryReport(
        client_name="prysm",
        new_guards=[
            VocabEntry(
                name="BlobsAreAvailable", category="state", description="d",
                evidence_file="beacon-chain/sync/x.go",
                evidence_function="DoIt", evidence_lines=[1, 9],
            ),
            VocabEntry(  # hallucinated file -> must be dropped
                name="MadeUpGuard", category="state", description="d",
                evidence_file="nonexistent/y.go",
            ),
        ],
        new_actions=[],
    )
    llm = _FakeLLM(plan, report)

    out = build_phase1_sub_agent("prysm", llm=llm)({"phase1_iteration": 1})
    reports = out["discovery_reports"]
    assert len(reports) == 1
    guards = reports[0]["new_guards"]
    names = {g["name"] for g in guards}
    assert "BlobsAreAvailable" in names
    assert "MadeUpGuard" not in names  # ungrounded -> dropped


def test_basename_fallback_match(monkeypatch):
    monkeypatch.setattr(
        "tools.search.search_codebase_by_workflow",
        lambda **kw: [_fake_result()],
    )
    plan = SearchPlan(queries=[SearchQuery(workflow_id="initial_sync", query="q")])
    report = VocabDiscoveryReport(
        client_name="prysm",
        new_guards=[VocabEntry(
            name="G", category="state", description="d",
            evidence_file="x.go",  # basename matches retrieved file
        )],
        new_actions=[],
    )
    out = build_phase1_sub_agent("prysm", llm=_FakeLLM(plan, report))({"phase1_iteration": 1})
    assert {g["name"] for g in out["discovery_reports"][0]["new_guards"]} == {"G"}


def test_no_snippets_returns_empty(monkeypatch):
    monkeypatch.setattr("tools.search.search_codebase_by_workflow", lambda **kw: [])
    monkeypatch.setattr("tools.search.search_codebase", lambda *a, **k: [])
    plan = SearchPlan(queries=[SearchQuery(workflow_id="initial_sync", query="q")])
    report = VocabDiscoveryReport(client_name="prysm", new_guards=[], new_actions=[])
    out = build_phase1_sub_agent("prysm", llm=_FakeLLM(plan, report))({"phase1_iteration": 1})
    assert out["discovery_reports"][0]["new_guards"] == []
    assert out["discovery_reports"][0]["new_actions"] == []


def test_mock_mode_no_llm_returns_empty():
    out = build_phase1_sub_agent("prysm", llm=None)({"phase1_iteration": 2})
    rep = out["discovery_reports"][0]
    assert rep["client_name"] == "prysm"
    assert rep["new_guards"] == [] and rep["new_actions"] == []
    assert rep["iteration"] == 2


def test_empty_plan_skips_retrieval(monkeypatch):
    called = {"n": 0}

    def _boom(**kw):
        called["n"] += 1
        return []

    monkeypatch.setattr("tools.search.search_codebase_by_workflow", _boom)
    plan = SearchPlan(queries=[])  # no queries
    report = VocabDiscoveryReport(client_name="prysm", new_guards=[], new_actions=[])
    out = build_phase1_sub_agent("prysm", llm=_FakeLLM(plan, report))({"phase1_iteration": 1})
    assert out["discovery_reports"][0]["new_guards"] == []
    assert called["n"] == 0  # retrieval never invoked
