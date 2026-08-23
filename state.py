"""Pydantic models and the LangGraph GlobalState TypedDict."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class VocabEntry(BaseModel):
    name: str
    category: str
    description: str
    evidence_file: str | None = None
    evidence_function: str | None = None
    evidence_lines: list[int] | None = None


class Evidence(BaseModel):
    file: str
    function: str
    lines: list[int] = Field(default_factory=list)


class MethodCall(BaseModel):
    """A single method invoked by a transition's action, with its own evidence.

    Populates ``Transition.method_calls`` so an action can be tied to the
    concrete call-site that performs it (F4). ``normalized_method`` is the
    canonical, vocabulary-aligned name used for cross-client matching.
    """

    name: str
    normalized_method: str = ""
    evidence: Evidence | None = None


# ── Parameter / behavior-divergence layer ────────────────────────────────────
# Captures constants, thresholds, and algorithm-shape differences across clients
# that do NOT fit the FSM ``Transition`` model (e.g. discv5 rate-limit values,
# peer-scoring weights/ban thresholds). See docs & the refactor plan.


class ParameterValue(BaseModel):
    """One client's value for one :class:`BehaviorAspect`."""

    client: str
    value: str                       # "250000", "per-IP 9/s + total 10/s", "absent", ...
    value_type: str = "literal"      # literal | absent | defined_but_unused | structural
    evidence: Evidence | None = None
    notes: str = ""


class BehaviorAspect(BaseModel):
    """One cross-client comparable row within a subsystem domain.

    e.g. ``id="discv5::udp_rate_limit"``, ``aspect_type="parameter"``.
    """

    id: str                          # "{domain_id}::{slug}"
    domain_id: str
    name: str
    description: str = ""
    aspect_type: str = "parameter"   # parameter | algorithm_shape | constant | presence
    unit: str = ""                   # "bytes/sec", "seconds", "score", ""
    per_client: dict[str, ParameterValue] = Field(default_factory=dict)


class ParameterDivergence(BaseModel):
    """The parameter-layer analogue of :class:`DiffItem`."""

    aspect_id: str                   # → BehaviorAspect.id
    domain_id: str
    name: str
    divergence_type: str             # value_split | presence_split | algorithm_shape | constant | default_config
    description: str
    severity: str = "MAJOR"          # CRITICAL | MAJOR | MINOR
    involved_clients: list[str] = Field(default_factory=list)
    deviating_clients: list[str] = Field(default_factory=list)
    per_client_values: dict[str, ParameterValue] = Field(default_factory=dict)
    value_groups: dict[str, list[str]] = Field(default_factory=dict)
    security_note: str = ""


class Transition(BaseModel):
    model_config = ConfigDict(extra="allow")

    guard: str
    actions: list[str] = Field(default_factory=list)
    next_state: str
    evidence: Evidence | None = None
    method_calls: list[MethodCall] = Field(default_factory=list)
    # Scenario IDs for which this transition is the handling point.
    # Populated by Phase 2.5 scenario scan; empty = no scenario annotation.
    scenario_ids: list[str] = Field(default_factory=list)
    # Classifies the nature of this transition:
    #   "normal"   — happy-path flow (default)
    #   "error"    — handles a failure, timeout, or rejection (e.g. peer ban,
    #                invalid payload, slashing protection failure, EL disconnect)
    #   "boundary" — triggered by a protocol-defined threshold, timing window,
    #                epoch/fork transition, capacity limit, or cutoff (e.g.
    #                epoch boundary process_epoch, 1/3-slot proposer boost,
    #                Deneb fork activation, optimistic depth limit exceeded)
    transition_type: str = "normal"


class LSGState(BaseModel):
    id: str
    label: str
    category: str
    transitions: list[Transition] = Field(default_factory=list)


class LSGWorkflow(BaseModel):
    id: str
    name: str
    description: str = ""
    mode: str = ""
    initial_state: str = ""
    states: list[LSGState] = Field(default_factory=list)


class LSGFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    client: str = ""
    generated_at: str = ""
    guards: list[VocabEntry] = Field(default_factory=list)
    actions: list[VocabEntry] = Field(default_factory=list)
    workflows: list[LSGWorkflow] = Field(default_factory=list)
    # Parameter/behavior values this client exhibits, keyed by aspect_id.
    behavior_aspects: dict[str, ParameterValue] = Field(default_factory=dict)


class DiffItem(BaseModel):
    workflow_id: str
    state_id: str
    transition_guard: str
    diff_type: str  # "A" or "B"
    description: str
    severity: str = ""  # CRITICAL / MAJOR / MINOR (B-class only)
    involved_clients: list[str] = Field(default_factory=list)
    deviating_clients: list[str] = Field(default_factory=list)
    security_note: str = ""
    # Tolerant on purpose: some providers (GLM) emit evidence as a flat
    # {file,function} dict instead of the {client: Evidence} mapping. Real
    # evidence is re-grounded in Phase 3, so accept any shape here.
    evidence: dict[str, Any] = Field(default_factory=dict)


class DiffReport(BaseModel):
    a_class_diffs: list[DiffItem] = Field(default_factory=list)
    b_class_diffs: list[DiffItem] = Field(default_factory=list)
    parameter_divergences: list[ParameterDivergence] = Field(default_factory=list)
    behavior_aspects: list[BehaviorAspect] = Field(default_factory=list)
    logic_diff_rate: float = 1.0
    total_transitions: int = 0


class VocabDiscoveryReport(BaseModel):
    client_name: str
    new_guards: list[VocabEntry] = Field(default_factory=list)
    new_actions: list[VocabEntry] = Field(default_factory=list)


class EnrichedSpec(BaseModel):
    version: int = 1
    guards: list[VocabEntry] = Field(default_factory=list)
    actions: list[VocabEntry] = Field(default_factory=list)


class PreprocessStatus(BaseModel):
    symbols_ready: bool = False
    callgraph_ready: bool = False
    vector_index_ready: bool = False
    bm25_index_ready: bool = False

    @property
    def all_ready(self) -> bool:
        return (self.symbols_ready and self.callgraph_ready
                and self.vector_index_ready and self.bm25_index_ready)


class ScenarioCoverage(BaseModel):
    """How one client handles one fault scenario, as found in its LSG.

    Produced by the Phase 2.5 scenario scan agent. If ``covered`` is False
    and ``suggested_transitions`` is non-empty, Phase 2 will be asked to
    re-iterate with those transitions as hints.
    """
    client: str
    scenario_id: str
    workflow_id: str
    covered: bool = False                   # scenario is represented in the LSG
    state_ids: list[str] = Field(default_factory=list)        # LSG state(s) that handle it
    transition_guards: list[str] = Field(default_factory=list)  # triggering guards
    evidence: Evidence | None = None
    notes: str = ""                          # brief description of the handling
    suggested_transitions: list[dict] = Field(default_factory=list)
    # ^ if covered=False, LLM proposes transition(s) to add in the next iteration


# ---- LangGraph reducers ------------------------------------------------

def _replace(_existing: Any, new: Any) -> Any:
    return new


def _merge_lists(existing: list, new: list) -> list:
    if existing is None:
        existing = []
    if new is None:
        new = []
    return existing + new


def _merge_vocab(existing: list, new: list) -> list:
    """Dedup by ``(name, category)``; newer entries replace older ones.

    F10: deduping by ``name`` alone silently dropped entries where two clients
    reuse a guard/action name with different semantics. Keying on
    ``(name, category)`` keeps same-name-different-category entries distinct.
    """
    if existing is None:
        existing = []
    if new is None:
        new = []

    def _key(entry: Any) -> tuple[str, str]:
        if isinstance(entry, dict):
            return (entry.get("name", ""), entry.get("category", ""))
        return (str(entry), "")

    seen: dict[tuple[str, str], int] = {}
    result: list = []
    for entry in existing:
        key = _key(entry)
        if key[0] and key not in seen:
            seen[key] = len(result)
            result.append(entry)
        elif not key[0]:
            result.append(entry)
    for entry in new:
        key = _key(entry)
        if key[0] and key in seen:
            result[seen[key]] = entry
        else:
            if key[0]:
                seen[key] = len(result)
            result.append(entry)
    return result


def _merge_dicts(existing: dict, new: dict) -> dict:
    if existing is None:
        existing = {}
    if new is None:
        new = {}
    return {**existing, **new}


class GlobalState(TypedDict, total=False):
    """LangGraph state. Uses reducers for fields written by parallel nodes."""

    current_phase: int
    phase1_iteration: int
    phase2_iteration: int

    guards: Annotated[list[dict], _merge_vocab]
    actions: Annotated[list[dict], _merge_vocab]
    vocab_version: int
    diff_rate: float

    client_lsgs: Annotated[dict[str, dict], _merge_dicts]

    diff_report: dict
    logic_diff_rate: float

    converged_phase1: bool
    converged_phase2: bool
    force_stopped: bool
    convergence_reason: str

    a_class_count: Annotated[int, _replace]
    prev_a_class_count: Annotated[int, _replace]
    iteration_history: Annotated[list[dict], _merge_lists]

    b_class_focus: bool
    b_class_focus_iteration: int
    prev_b_class_count: Annotated[int, _replace]

    preprocess_done: bool
    preprocess_status: Annotated[dict[str, dict], _merge_dicts]

    audit_log_paths: Annotated[list[str], _merge_lists]

    discovery_reports: Annotated[list[dict], _merge_lists]
    a_class_feedback: Annotated[list[dict], _replace]
    sparsity_hints: Annotated[list[dict], _replace]

    current_workflow: str
    completed_workflows: Annotated[list[str], _merge_lists]
    workflow_diff_reports: Annotated[dict[str, dict], _merge_dicts]
    wf_iteration_history: Annotated[list[dict], _replace]

    verified_b_diffs: Annotated[list[dict], _merge_lists]
    rejected_b_diffs: Annotated[list[dict], _merge_lists]
    unverified_b_diffs: Annotated[list[dict], _merge_lists]
    reclassified_to_a: Annotated[list[dict], _merge_lists]
    verification_evidence: Annotated[dict[str, list], _merge_dicts]

    # Phase 2.5 — scenario scan
    # scenario_coverages: { "{client}::{wf}::{scenario_id}" → ScenarioCoverage dict }
    scenario_coverages: Annotated[dict[str, dict], _merge_dicts]
    # which (workflow_id, scenario_id) pairs have already triggered a re-iterate
    scenario_triggered_reiter: Annotated[list[str], _merge_lists]

    # ── Parameter / behavior-divergence layer (subsystem domains) ──────────
    current_domain: str
    completed_domains: Annotated[list[str], _merge_lists]
    domain_diff_reports: Annotated[dict[str, dict], _merge_dicts]
    # {client: {aspect_id: ParameterValue dict}}
    client_aspects: Annotated[dict[str, dict], _merge_dicts]
    parameter_divergences: Annotated[list[dict], _merge_lists]
    behavior_aspects: Annotated[dict[str, dict], _merge_dicts]   # keyed by aspect_id

    # ── Phase 1 failure visibility (R4) ─────────────────────────────────────
    phase1_sub_errors: Annotated[int, operator.add]
    phase1_sub_failed_clients: Annotated[list[str], _merge_lists]

