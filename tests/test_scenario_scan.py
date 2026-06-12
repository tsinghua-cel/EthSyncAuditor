"""Tests for Phase 2.5 scenario scan logic."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from agents.phase2_scenario_agent import (
    _annotate_lsg,
    get_scenario_hints_for_reiter,
)
from config import SCENARIOS


# ── _annotate_lsg ─────────────────────────────────────────────────────────────


def _make_lsg(wf_id: str, guard: str) -> dict:
    return {
        "workflows": [{
            "id": wf_id,
            "states": [{
                "id": f"{wf_id}.s1",
                "label": "S1", "category": "init",
                "transitions": [{"guard": guard, "actions": [], "next_state": "done"}],
            }],
        }],
    }


def test_annotate_adds_scenario_id():
    lsg = _make_lsg("initial_sync", "IsSyncStalled")
    _annotate_lsg(lsg, "initial_sync", ["IsSyncStalled"], "sync_stall")
    tr = lsg["workflows"][0]["states"][0]["transitions"][0]
    assert tr.get("scenario_ids") == ["sync_stall"]


def test_annotate_no_duplicate():
    lsg = _make_lsg("initial_sync", "IsSyncStalled")
    _annotate_lsg(lsg, "initial_sync", ["IsSyncStalled"], "sync_stall")
    _annotate_lsg(lsg, "initial_sync", ["IsSyncStalled"], "sync_stall")
    tr = lsg["workflows"][0]["states"][0]["transitions"][0]
    assert tr["scenario_ids"].count("sync_stall") == 1


def test_annotate_wrong_guard_not_tagged():
    lsg = _make_lsg("initial_sync", "SomethingElse")
    _annotate_lsg(lsg, "initial_sync", ["IsSyncStalled"], "sync_stall")
    tr = lsg["workflows"][0]["states"][0]["transitions"][0]
    assert not tr.get("scenario_ids")


# ── get_scenario_hints_for_reiter ─────────────────────────────────────────────


def _coverage(client: str, wf: str, sid: str, covered: bool) -> dict:
    return {
        "client": client,
        "scenario_id": sid,
        "workflow_id": wf,
        "covered": covered,
        "notes": "test",
    }


def test_hints_returned_for_uncovered():
    state = {
        "scenario_coverages": {
            "prysm::initial_sync::sync_stall": _coverage(
                "prysm", "initial_sync", "sync_stall", False
            ),
        },
        "scenario_triggered_reiter": [],
    }
    hints = get_scenario_hints_for_reiter(state, "initial_sync")
    assert len(hints) == 1
    assert hints[0]["scenario_id"] == "sync_stall"


def test_no_hints_for_covered():
    state = {
        "scenario_coverages": {
            "prysm::initial_sync::sync_stall": _coverage(
                "prysm", "initial_sync", "sync_stall", True
            ),
        },
        "scenario_triggered_reiter": [],
    }
    hints = get_scenario_hints_for_reiter(state, "initial_sync")
    assert hints == []


def test_no_hints_for_already_triggered():
    state = {
        "scenario_coverages": {
            "prysm::initial_sync::sync_stall": _coverage(
                "prysm", "initial_sync", "sync_stall", False
            ),
        },
        "scenario_triggered_reiter": ["initial_sync::sync_stall"],
    }
    hints = get_scenario_hints_for_reiter(state, "initial_sync")
    assert hints == []


def test_no_hints_for_different_workflow():
    state = {
        "scenario_coverages": {
            "prysm::regular_sync::sync_stall": _coverage(
                "prysm", "regular_sync", "sync_stall", False
            ),
        },
        "scenario_triggered_reiter": [],
    }
    hints = get_scenario_hints_for_reiter(state, "initial_sync")
    assert hints == []


# ── config: scenario relevant_workflows ─────────────────────────────────────


def test_all_scenarios_have_relevant_workflows():
    for sc in SCENARIOS:
        assert sc.relevant_workflows, f"Scenario {sc.id} has no relevant_workflows"
        assert sc.search_queries, f"Scenario {sc.id} has no search_queries"


def test_scenario_ids_unique():
    ids = [sc.id for sc in SCENARIOS]
    assert len(ids) == len(set(ids)), "Duplicate scenario IDs"


def test_scenarios_cover_all_workflows():
    from config import WORKFLOW_IDS
    covered = {wf for sc in SCENARIOS for wf in sc.relevant_workflows}
    # All workflows should have at least one scenario (except aggregate which is
    # lower risk — we can accept it having none)
    for wf in WORKFLOW_IDS:
        if wf == "aggregate":
            continue
        assert wf in covered, f"Workflow {wf} has no scenario coverage"
