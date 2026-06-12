"""LangGraph definition for the EthSyncAuditor pipeline.

Topology:
    preprocess
    → phase1 (fanout ×5 → sub → main → loop until convergence)
    → workflow_scheduler
        → phase2 (fanout ×5 → sub → main → loop)
        → phase3 verification (fanout × involved clients → sub → main)
        → next workflow
    → final_aggregate
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from langgraph.graph import END, StateGraph
from langgraph.types import Send

import config
from config import CLIENT_NAMES, WORKFLOW_IDS
from state import GlobalState

logger = logging.getLogger(__name__)

_last_p2_convergence_reason: str = ""

_graph_config: dict[str, Any] = {
    "llm": None,
    "mock": True,
    "callbacks": None,
}


def configure_graph(*, llm: Any = None, mock: bool = True,
                    callbacks: list[Any] | None = None) -> None:
    _graph_config["llm"] = llm
    _graph_config["mock"] = mock
    _graph_config["callbacks"] = callbacks


def get_graph_config() -> dict[str, Any]:
    return dict(_graph_config)


def make_initial_state() -> dict[str, Any]:
    return {
        "current_phase": 0,
        "phase1_iteration": 1,
        "phase2_iteration": 0,
        "guards": [],
        "actions": [],
        "vocab_version": 0,
        "diff_rate": 1.0,
        "client_lsgs": {},
        "diff_report": {},
        "logic_diff_rate": 1.0,
        "converged_phase1": False,
        "converged_phase2": False,
        "force_stopped": False,
        "convergence_reason": "",
        "a_class_count": -1,
        "prev_a_class_count": -1,
        "iteration_history": [],
        "preprocess_done": False,
        "preprocess_status": {},
        "audit_log_paths": [],
        "discovery_reports": [],
        "a_class_feedback": [],
        "sparsity_hints": [],
        "b_class_focus": False,
        "b_class_focus_iteration": 0,
        "prev_b_class_count": -1,
        "current_workflow": "",
        "completed_workflows": [],
        "workflow_diff_reports": {},
        "wf_iteration_history": [],
        "verified_b_diffs": [],
        "rejected_b_diffs": [],
        "unverified_b_diffs": [],
        "reclassified_to_a": [],
        "verification_evidence": {},
        "scenario_coverages": {},
        "scenario_triggered_reiter": [],
    }


def _get_llm() -> Any:
    if _graph_config["mock"]:
        return None
    return _graph_config["llm"]


def _make_callbacks(phase: int, iteration: int, agent_type: str) -> list[Any] | None:
    if _graph_config["mock"]:
        return None

    from file_io.audit_logger import AuditLogCallback

    base = list(_graph_config.get("callbacks") or [])
    base = [cb for cb in base if not isinstance(cb, AuditLogCallback)]
    base.append(AuditLogCallback(phase=phase, iteration=iteration, agent_type=agent_type))
    return base or None


def preprocess_node(state: GlobalState) -> dict[str, Any]:
    if state.get("preprocess_done"):
        return {}

    if not _graph_config["mock"]:
        from tools.preprocessor import run_all_preprocessing
        statuses = run_all_preprocessing(force_rebuild=False)
    else:
        statuses = {
            client: {
                "symbols_ready": True,
                "callgraph_ready": True,
                "vector_index_ready": True,
                "bm25_index_ready": True,
            }
            for client in CLIENT_NAMES
        }

    return {"preprocess_done": True, "preprocess_status": statuses}


def phase1_sub_agent_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase1_sub_agent import build_phase1_sub_agent

    client_name = state.get("_client_name", "unknown")
    iteration = state.get("phase1_iteration", 1)
    logger.info("[phase1_sub] client=%s iter=%d", client_name, iteration)

    cbs = _make_callbacks(1, iteration, f"phase1_sub_{client_name}")
    return build_phase1_sub_agent(client_name, llm=_get_llm(), callbacks=cbs)(state)


def phase1_main_agent_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase1_main_agent import build_phase1_main_agent
    from file_io.checkpoint import save_checkpoint

    iteration = state.get("phase1_iteration", 1)
    reports = state.get("discovery_reports", [])
    logger.info("[phase1_main] iter=%d reports=%d", iteration, len(reports))

    cbs = _make_callbacks(1, iteration, "phase1_main")
    result = build_phase1_main_agent(llm=_get_llm(), callbacks=cbs)(state)

    try:
        save_checkpoint({**state, **result}, phase=1, iteration=iteration)
    except Exception:
        logger.warning("[phase1_main] checkpoint save failed", exc_info=True)

    return result


def phase1_fanout(state: GlobalState) -> list[Send]:
    return [
        Send("phase1_sub_agent", {**state, "_client_name": client})
        for client in CLIENT_NAMES
    ]


def route_after_phase1_main(state: GlobalState) -> str:
    iteration = state.get("phase1_iteration", 1)
    diff_rate = state.get("diff_rate", 1.0)

    if diff_rate < config.CONVERGENCE_THRESHOLD:
        logger.info("[router_phase1] converged iter=%d diff_rate=%.4f",
                    iteration, diff_rate)
        return "phase1_done"
    if iteration >= config.MAX_ITER_PHASE1:
        logger.warning("[router_phase1] max iterations reached (diff_rate=%.4f)",
                       diff_rate)
        return "phase1_done"
    return "phase1_next_iter"


def phase1_next_iter_node(state: GlobalState) -> dict[str, Any]:
    return {"phase1_iteration": state.get("phase1_iteration", 1) + 1}


def phase1_done_node(state: GlobalState) -> dict[str, Any]:
    guards = state.get("guards", [])
    actions = state.get("actions", [])
    logger.info("[phase1_done] guards=%d actions=%d", len(guards), len(actions))

    try:
        out_path = config.OUTPUT_PATH / "Global_LSG_Spec_Enriched.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"guards": guards, "actions": actions},
                           f, sort_keys=False, allow_unicode=True)
    except Exception:
        logger.warning("[phase1_done] failed to write vocab yaml", exc_info=True)

    return {
        "converged_phase1": True,
        "current_phase": 2,
        "client_lsgs": {},
    }


def workflow_scheduler_node(state: GlobalState) -> dict[str, Any]:
    completed = state.get("completed_workflows", [])
    for wf_id in WORKFLOW_IDS:
        if wf_id not in completed:
            logger.info("[workflow_scheduler] next=%s completed=%s", wf_id, completed)
            return {
                "current_workflow": wf_id,
                "phase2_iteration": 1,
                "a_class_count": -1,
                "prev_a_class_count": -1,
                "wf_iteration_history": [],
                "b_class_focus": False,
                "b_class_focus_iteration": 0,
                "prev_b_class_count": -1,
                "diff_report": {},
                "a_class_feedback": [],
                "sparsity_hints": [],
            }

    logger.info("[workflow_scheduler] all %d workflows done", len(WORKFLOW_IDS))
    return {"current_workflow": ""}


def route_after_workflow_scheduler(state: GlobalState) -> str:
    return "phase2_fanout" if state.get("current_workflow") else "final_aggregate"


def phase2_sub_agent_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase2_sub_agent import build_phase2_sub_agent
    from file_io.writer import write_client_lsg

    client_name = state.get("_client_name", "unknown")
    iteration = state.get("phase2_iteration", 1)
    current_wf = state.get("current_workflow", "unknown")
    logger.info("[phase2_sub] client=%s wf=%s iter=%d",
                client_name, current_wf, iteration)

    cbs = _make_callbacks(2, iteration, f"phase2_sub_{client_name}_{current_wf}")
    result = build_phase2_sub_agent(client_name, llm=_get_llm(), callbacks=cbs)(state)

    lsg = result.get("client_lsgs", {}).get(client_name)
    if lsg is not None:
        try:
            write_client_lsg(client_name,
                             {**lsg, "_iteration": iteration, "_workflow": current_wf},
                             final=False)
        except Exception:
            logger.warning("[phase2_sub] intermediate LSG write failed", exc_info=True)

    return result


def phase2_main_agent_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase2_main_agent import build_phase2_main_agent
    from file_io.checkpoint import save_checkpoint

    iteration = state.get("phase2_iteration", 1)
    current_wf = state.get("current_workflow", "unknown")
    client_lsgs = state.get("client_lsgs", {})
    logger.info("[phase2_main] wf=%s iter=%d clients=%d",
                current_wf, iteration, len(client_lsgs))

    cbs = _make_callbacks(2, iteration, f"phase2_main_{current_wf}")
    result = build_phase2_main_agent(llm=_get_llm(), callbacks=cbs)(state)

    diff_report = result.get("diff_report", {})
    a_count = result.get("a_class_count", len(diff_report.get("a_class_diffs", [])))
    b_count = len(diff_report.get("b_class_diffs", []))
    metric = {
        "workflow": current_wf,
        "iteration": iteration,
        "a_class_count": a_count,
        "b_class_count": b_count,
        "logic_diff_rate": result.get("logic_diff_rate", 0.0),
    }
    result["wf_iteration_history"] = list(state.get("wf_iteration_history", [])) + [metric]
    result["iteration_history"] = [metric]

    try:
        save_checkpoint({**state, **result}, phase=2, iteration=iteration)
    except Exception:
        logger.warning("[phase2_main] checkpoint save failed", exc_info=True)

    return result


def phase2_fanout(state: GlobalState) -> list[Send]:
    return [
        Send("phase2_sub_agent", {**state, "_client_name": client})
        for client in CLIENT_NAMES
    ]


def route_after_phase2_main(state: GlobalState) -> str:
    global _last_p2_convergence_reason

    iteration = state.get("phase2_iteration", 1)
    current_wf = state.get("current_workflow", "?")
    a_class_count = state.get("a_class_count", -1)
    prev_a_class_count = state.get("prev_a_class_count", -1)
    history = state.get("wf_iteration_history", [])
    b_class_focus = state.get("b_class_focus", False)
    b_class_focus_iteration = state.get("b_class_focus_iteration", 0)

    if b_class_focus:
        diff_report = state.get("diff_report", {})
        b_count = len(diff_report.get("b_class_diffs", []))

        if state.get("prev_b_class_count", -1) >= 0:
            recent_b = (
                [h.get("b_class_count", 0) for h in history[-config.B_CLASS_STABLE_WINDOW:]]
                if len(history) >= config.B_CLASS_STABLE_WINDOW else []
            )
            if (recent_b
                    and max(recent_b) - min(recent_b) <= config.B_CLASS_CHANGE_THRESHOLD):
                _last_p2_convergence_reason = (
                    f"[{current_wf}] B-class converged at iter {iteration} "
                    f"(b_iter {b_class_focus_iteration}): stable at {b_count} ({recent_b})"
                )
                logger.info("[router_phase2] %s B-class converged: %s", current_wf, recent_b)
                return "phase2_wf_converged"

        if b_class_focus_iteration >= config.MAX_ITER_B_CLASS:
            _last_p2_convergence_reason = (
                f"[{current_wf}] MAX_ITER_B_CLASS reached at iter {iteration}, "
                f"B-class={b_count}"
            )
            return "phase2_wf_converged"

        return "phase2_next_iter"

    if a_class_count == 0:
        _last_p2_convergence_reason = (
            f"[{current_wf}] zero A-class diffs at iter {iteration}; entering B-class"
        )
        return "phase2_enter_b_class_focus"

    if prev_a_class_count >= 0 and a_class_count >= 0:
        delta_rate = abs(a_class_count - prev_a_class_count) / max(prev_a_class_count, 1)
        if delta_rate < config.P2_A_CLASS_CONVERGENCE_THRESHOLD:
            _last_p2_convergence_reason = (
                f"[{current_wf}] A-class stabilized at iter {iteration} "
                f"(prev={prev_a_class_count}, cur={a_class_count}, "
                f"rate={delta_rate:.4f}); entering B-class"
            )
            return "phase2_enter_b_class_focus"

    if len(history) >= config.OSCILLATION_WINDOW:
        recent_a = [h.get("a_class_count", 0) for h in history[-config.OSCILLATION_WINDOW:]]
        if max(recent_a) - min(recent_a) <= config.OSCILLATION_BAND:
            _last_p2_convergence_reason = (
                f"[{current_wf}] A-class oscillating at iter {iteration}: {recent_a}; "
                f"entering B-class"
            )
            return "phase2_enter_b_class_focus"

    if iteration >= config.MAX_ITER_PHASE2:
        _last_p2_convergence_reason = (
            f"[{current_wf}] MAX_ITER_PHASE2 reached, A-class={a_class_count}"
        )
        logger.warning("[router_phase2] %s force-stopping", current_wf)
        return "phase2_wf_force_stop"

    _last_p2_convergence_reason = ""
    return "phase2_next_iter"


def phase2_next_iter_node(state: GlobalState) -> dict[str, Any]:
    diff_report = state.get("diff_report", {})
    result: dict[str, Any] = {
        "phase2_iteration": state.get("phase2_iteration", 1) + 1,
        "prev_a_class_count": state.get("a_class_count", -1),
        "prev_b_class_count": len(diff_report.get("b_class_diffs", [])),
    }
    if state.get("b_class_focus"):
        result["b_class_focus_iteration"] = state.get("b_class_focus_iteration", 0) + 1
    return result


def phase2_enter_b_class_focus_node(state: GlobalState) -> dict[str, Any]:
    diff_report = state.get("diff_report", {})
    b_count = len(diff_report.get("b_class_diffs", []))
    logger.info("[phase2_enter_b_class] wf=%s B-class=%d",
                state.get("current_workflow", "?"), b_count)
    return {
        "b_class_focus": True,
        "b_class_focus_iteration": 1,
        "prev_b_class_count": b_count,
        "phase2_iteration": state.get("phase2_iteration", 1) + 1,
        "prev_a_class_count": state.get("a_class_count", -1),
    }


def phase2_wf_converged_node(state: GlobalState) -> dict[str, Any]:
    wf = state.get("current_workflow", "")
    logger.info("[phase2_wf_converged] wf=%s", wf)
    return {
        "completed_workflows": [wf],
        "workflow_diff_reports": {wf: state.get("diff_report", {})},
        "convergence_reason": _last_p2_convergence_reason,
    }


def phase2_wf_force_stop_node(state: GlobalState) -> dict[str, Any]:
    wf = state.get("current_workflow", "")
    logger.warning("[phase2_wf_force_stop] wf=%s", wf)
    return {
        "completed_workflows": [wf],
        "workflow_diff_reports": {wf: state.get("diff_report", {})},
        "convergence_reason": _last_p2_convergence_reason,
    }


# ── Phase 2.5: Scenario Scan ─────────────────────────────────────────────────


def phase2_scenario_fanout(state: GlobalState) -> list[Send]:
    """Fan out to one scenario-scan sub-agent per client."""
    return [
        Send("phase2_scenario_sub", {**state, "_client_name": client})
        for client in CLIENT_NAMES
    ]


def phase2_scenario_sub_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase2_scenario_agent import build_phase2_scenario_agent

    client_name = state.get("_client_name", "unknown")
    current_wf = state.get("current_workflow", "unknown")
    logger.info("[phase2_scenario_sub] client=%s wf=%s", client_name, current_wf)

    cbs = _make_callbacks(2, 0, f"phase2_scenario_{client_name}_{current_wf}")
    return build_phase2_scenario_agent(
        client_name, llm=_get_llm(), callbacks=cbs,
    )(state)


def phase2_scenario_collect_node(state: GlobalState) -> dict[str, Any]:
    """Collect scenario scan results and decide whether to re-iterate Phase 2."""
    from agents.phase2_scenario_agent import get_scenario_hints_for_reiter

    wf = state.get("current_workflow", "")
    coverages = state.get("scenario_coverages", {})
    triggered = set(state.get("scenario_triggered_reiter", []))

    # Count per-scenario coverage across clients
    from config import SCENARIOS
    relevant = [s for s in SCENARIOS if wf in s.relevant_workflows]
    summary: list[str] = []
    for sc in relevant:
        covered_clients = [
            cov["client"]
            for key, cov in coverages.items()
            if cov.get("scenario_id") == sc.id
            and cov.get("workflow_id") == wf
            and cov.get("covered")
        ]
        summary.append(
            f"  {sc.id}: covered_by={covered_clients or 'NONE'}"
        )
    if summary:
        logger.info("[phase2_scenario_collect] wf=%s\n%s", wf, "\n".join(summary))

    # Build sparsity-style hints for uncovered scenarios
    hints = get_scenario_hints_for_reiter(state, wf)
    new_reiter_keys = [
        f"{wf}::{h['scenario_id']}"
        for h in hints
        if f"{wf}::{h['scenario_id']}" not in triggered
    ]

    if hints and new_reiter_keys:
        logger.info(
            "[phase2_scenario_collect] wf=%s — scenario gaps for %d scenarios, "
            "triggering re-iteration",
            wf, len({h["scenario_id"] for h in hints}),
        )
        return {
            "sparsity_hints": hints,
            "scenario_triggered_reiter": new_reiter_keys,
        }

    return {}


def _route_after_scenario_collect(state: GlobalState) -> str:
    """After scenario scan: re-iterate Phase 2 if NEW gaps found, else proceed."""
    wf = state.get("current_workflow", "")
    triggered = set(state.get("scenario_triggered_reiter", []))
    hints = state.get("sparsity_hints", [])

    # Only look at scenario-type hints (they have a 'scenario_id' field) for
    # the current workflow. Regular sparsity hints from Phase 2 don't have
    # 'scenario_id' and should not trigger a re-iteration here.
    wf_scenario_hints = [
        h for h in hints
        if h.get("workflow_id") == wf and h.get("scenario_id")
    ]

    # A re-iteration is warranted only if there are un-triggered scenario gaps
    new_gaps = any(
        f"{wf}::{h['scenario_id']}" not in triggered
        for h in wf_scenario_hints
    )
    if new_gaps:
        logger.info("[route_scenario] wf=%s → re-enter phase2_fanout", wf)
        return "phase2_fanout"
    return "verify_or_scheduler"


def phase3_verify_fanout(state: GlobalState) -> list[Send]:
    diff_report = state.get("diff_report", {})
    b_diffs = diff_report.get("b_class_diffs", [])

    if not b_diffs:
        return [Send("phase3_verify_main", {**state, "_verify_b_diffs": []})]

    involved: set[str] = set()
    for d in b_diffs:
        involved.update(d.get("deviating_clients", []))
        involved.update(d.get("involved_clients", []))
    if not involved:
        involved = set(CLIENT_NAMES)

    logger.info("[phase3_verify_fanout] wf=%s diffs=%d clients=%d",
                state.get("current_workflow", "?"), len(b_diffs), len(involved))

    return [
        Send("phase3_verify_sub",
             {**state, "_client_name": client, "_verify_b_diffs": b_diffs})
        for client in sorted(involved)
    ]


def phase3_verify_sub_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase3_verify_agent import build_phase3_verify_sub_agent

    client_name = state.get("_client_name", "unknown")
    current_wf = state.get("current_workflow", "unknown")
    logger.info("[phase3_verify_sub] client=%s wf=%s", client_name, current_wf)

    cbs = _make_callbacks(3, 0, f"phase3_verify_sub_{client_name}_{current_wf}")
    return build_phase3_verify_sub_agent(client_name, llm=_get_llm(), callbacks=cbs)(state)


def phase3_verify_main_node(state: GlobalState) -> dict[str, Any]:
    from agents.phase3_verify_agent import build_phase3_verify_main_agent

    current_wf = state.get("current_workflow", "unknown")
    b_diffs = (state.get("_verify_b_diffs", [])
               or state.get("diff_report", {}).get("b_class_diffs", []))
    logger.info("[phase3_verify_main] wf=%s diffs=%d", current_wf, len(b_diffs))

    cbs = _make_callbacks(3, 0, f"phase3_verify_main_{current_wf}")
    agent_fn = build_phase3_verify_main_agent(llm=_get_llm(), callbacks=cbs)
    return agent_fn({**state, "_verify_b_diffs": b_diffs})


def phase3_wf_verified_node(state: GlobalState) -> dict[str, Any]:
    wf = state.get("current_workflow", "")
    report = dict(state.get("workflow_diff_reports", {}).get(wf, {}))

    wf_verified = [d for d in state.get("verified_b_diffs", [])
                   if d.get("workflow_id") == wf]
    if wf_verified:
        report["b_class_diffs"] = wf_verified

    wf_reclassified = [d for d in state.get("reclassified_to_a", [])
                       if d.get("workflow_id") == wf]
    if wf_reclassified:
        report["a_class_diffs"] = list(report.get("a_class_diffs", [])) + wf_reclassified

    n_a = len(report.get("a_class_diffs", []))
    n_b = len(report.get("b_class_diffs", []))
    report["logic_diff_rate"] = n_b / max(n_a + n_b, 1)

    logger.info("[phase3_wf_verified] wf=%s verified=%d reclassified=%d",
                wf, len(wf_verified), len(wf_reclassified))
    return {"workflow_diff_reports": {wf: report}}


def final_aggregate_node(state: GlobalState) -> dict[str, Any]:
    wf_reports = state.get("workflow_diff_reports", {})
    all_a: list[dict] = []
    all_b: list[dict] = []
    total = 0
    for wf_id in WORKFLOW_IDS:
        report = wf_reports.get(wf_id, {})
        all_a.extend(report.get("a_class_diffs", []))
        all_b.extend(report.get("b_class_diffs", []))
        total += report.get("total_transitions", 0)

    logic_diff_rate = len(all_b) / max(total, 1)

    n_confirmed = sum(1 for d in all_b if d.get("verification_status") == "CONFIRMED")
    n_downgraded = sum(1 for d in all_b if d.get("verification_status") == "DOWNGRADED")
    logger.info(
        "[final_aggregate] wfs=%d A=%d B=%d (confirmed=%d downgraded=%d) total=%d rate=%.4f",
        len(wf_reports), len(all_a), len(all_b), n_confirmed, n_downgraded,
        total, logic_diff_rate,
    )

    return {
        "diff_report": {
            "a_class_diffs": all_a,
            "b_class_diffs": all_b,
            "logic_diff_rate": logic_diff_rate,
            "total_transitions": total,
        },
        "logic_diff_rate": logic_diff_rate,
        "converged_phase2": True,
    }


def _route_to_verify_or_scheduler(state: GlobalState) -> str:
    return "phase3_verify_fanout" if config.VERIFY_ENABLED else "workflow_scheduler"


def build_graph() -> StateGraph:
    g = StateGraph(GlobalState)

    g.add_node("preprocess", preprocess_node)
    g.add_node("phase1_fanout", lambda _s: {})
    g.add_node("phase1_sub_agent", phase1_sub_agent_node)
    g.add_node("phase1_main_agent", phase1_main_agent_node)
    g.add_node("phase1_next_iter", phase1_next_iter_node)
    g.add_node("phase1_done", phase1_done_node)
    g.add_node("workflow_scheduler", workflow_scheduler_node)
    g.add_node("phase2_fanout", lambda _s: {})
    g.add_node("phase2_sub_agent", phase2_sub_agent_node)
    g.add_node("phase2_main_agent", phase2_main_agent_node)
    g.add_node("phase2_next_iter", phase2_next_iter_node)
    g.add_node("phase2_enter_b_class_focus", phase2_enter_b_class_focus_node)
    g.add_node("phase2_wf_converged", phase2_wf_converged_node)
    g.add_node("phase2_wf_force_stop", phase2_wf_force_stop_node)
    g.add_node("phase2_scenario_fanout", lambda _s: {})
    g.add_node("phase2_scenario_sub", phase2_scenario_sub_node)
    g.add_node("phase2_scenario_collect", phase2_scenario_collect_node)
    g.add_node("phase3_verify_fanout", lambda _s: {})
    g.add_node("phase3_verify_sub", phase3_verify_sub_node)
    g.add_node("phase3_verify_main", phase3_verify_main_node)
    g.add_node("phase3_wf_verified", phase3_wf_verified_node)
    g.add_node("final_aggregate", final_aggregate_node)

    g.set_entry_point("preprocess")

    g.add_edge("preprocess", "phase1_fanout")
    g.add_conditional_edges("phase1_fanout", phase1_fanout, ["phase1_sub_agent"])
    g.add_edge("phase1_sub_agent", "phase1_main_agent")
    g.add_conditional_edges(
        "phase1_main_agent", route_after_phase1_main,
        {"phase1_next_iter": "phase1_next_iter", "phase1_done": "phase1_done"},
    )
    g.add_conditional_edges(
        "phase1_next_iter", lambda _s: "phase1_fanout",
        {"phase1_fanout": "phase1_fanout"},
    )
    g.add_edge("phase1_done", "workflow_scheduler")

    g.add_conditional_edges(
        "workflow_scheduler", route_after_workflow_scheduler,
        {"phase2_fanout": "phase2_fanout", "final_aggregate": "final_aggregate"},
    )
    g.add_conditional_edges("phase2_fanout", phase2_fanout, ["phase2_sub_agent"])
    g.add_edge("phase2_sub_agent", "phase2_main_agent")
    g.add_conditional_edges(
        "phase2_main_agent", route_after_phase2_main,
        {
            "phase2_wf_converged": "phase2_wf_converged",
            "phase2_wf_force_stop": "phase2_wf_force_stop",
            "phase2_next_iter": "phase2_next_iter",
            "phase2_enter_b_class_focus": "phase2_enter_b_class_focus",
        },
    )
    g.add_conditional_edges(
        "phase2_next_iter", lambda _s: "phase2_fanout",
        {"phase2_fanout": "phase2_fanout"},
    )
    g.add_conditional_edges(
        "phase2_enter_b_class_focus", lambda _s: "phase2_fanout",
        {"phase2_fanout": "phase2_fanout"},
    )
    for end_node in ("phase2_wf_converged", "phase2_wf_force_stop"):
        g.add_edge(end_node, "phase2_scenario_fanout")

    g.add_conditional_edges("phase2_scenario_fanout", phase2_scenario_fanout,
                            ["phase2_scenario_sub"])
    g.add_edge("phase2_scenario_sub", "phase2_scenario_collect")
    g.add_conditional_edges(
        "phase2_scenario_collect", _route_after_scenario_collect,
        {"phase2_fanout": "phase2_fanout", "verify_or_scheduler": "verify_or_scheduler"},
    )
    # Proxy node so we can use it as a conditional target
    g.add_node("verify_or_scheduler", lambda s: {})
    g.add_conditional_edges(
        "verify_or_scheduler", _route_to_verify_or_scheduler,
        {
            "phase3_verify_fanout": "phase3_verify_fanout",
            "workflow_scheduler": "workflow_scheduler",
        },
    )

    g.add_conditional_edges("phase3_verify_fanout", phase3_verify_fanout,
                            ["phase3_verify_sub"])
    g.add_edge("phase3_verify_sub", "phase3_verify_main")
    g.add_edge("phase3_verify_main", "phase3_wf_verified")
    g.add_edge("phase3_wf_verified", "workflow_scheduler")

    g.add_edge("final_aggregate", END)
    return g


def compile_graph():
    return build_graph().compile()

