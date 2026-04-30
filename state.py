"""Pydantic models and the LangGraph GlobalState TypedDict."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


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


class Transition(BaseModel):
    guard: str
    actions: list[str] = Field(default_factory=list)
    next_state: str
    evidence: Evidence | None = None


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
    version: int = 1
    client: str = ""
    generated_at: str = ""
    guards: list[VocabEntry] = Field(default_factory=list)
    actions: list[VocabEntry] = Field(default_factory=list)
    workflows: list[LSGWorkflow] = Field(default_factory=list)


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
    evidence: dict[str, Evidence | None] = Field(default_factory=dict)


class DiffReport(BaseModel):
    a_class_diffs: list[DiffItem] = Field(default_factory=list)
    b_class_diffs: list[DiffItem] = Field(default_factory=list)
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
    """Dedup by ``name``; newer entries replace older ones."""
    if existing is None:
        existing = []
    if new is None:
        new = []
    seen: dict[str, int] = {}
    result: list = []
    for entry in existing:
        name = entry.get("name", "") if isinstance(entry, dict) else str(entry)
        if name and name not in seen:
            seen[name] = len(result)
            result.append(entry)
        elif not name:
            result.append(entry)
    for entry in new:
        name = entry.get("name", "") if isinstance(entry, dict) else str(entry)
        if name and name in seen:
            result[seen[name]] = entry
        else:
            if name:
                seen[name] = len(result)
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
    reclassified_to_a: Annotated[list[dict], _merge_lists]
    verification_evidence: Annotated[dict[str, list], _merge_dicts]

