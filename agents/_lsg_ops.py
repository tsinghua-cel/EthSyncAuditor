"""Shared LSG workflow operations.

Collapses the duplicated workflow lookup/replace/serialize/annotate logic
across phase2_sub and phase2_scenario.
"""

from __future__ import annotations

import yaml


def extract_workflow(lsg: dict, wf_id: str) -> dict | None:
    """Return the workflow dict with ``id == wf_id``, or None."""
    for wf in lsg.get("workflows", []):
        if wf.get("id") == wf_id:
            return wf
    return None


def replace_workflow(lsg: dict, wf_id: str, new_wf: dict) -> dict:
    """Return a copy of *lsg* with the ``wf_id`` workflow substituted/appended."""
    updated = dict(lsg)
    new_workflows: list[dict] = []
    replaced = False
    for wf in lsg.get("workflows", []):
        if wf.get("id") == wf_id:
            new_workflows.append(new_wf)
            replaced = True
        else:
            new_workflows.append(wf)
    if not replaced:
        new_workflows.append(new_wf)
    updated["workflows"] = new_workflows
    return updated


def serialize_workflow_yaml(wf: dict, *, keep_evidence: bool = True) -> str:
    """Serialize one workflow dict to compact YAML.

    When *keep_evidence* is False, the ``evidence`` field is stripped from
    every transition (legacy redact-for-feedback behaviour; now unused — the
    feedback path keeps evidence so the grounding loop stays intact).
    """
    wf_copy = dict(wf)
    if not keep_evidence:
        new_states = []
        for st in wf_copy.get("states", []):
            st_copy = dict(st)
            new_trans = [
                {k: v for k, v in tr.items() if k != "evidence"}
                for tr in st_copy.get("transitions", [])
            ]
            st_copy["transitions"] = new_trans
            new_states.append(st_copy)
        wf_copy["states"] = new_states
    return yaml.dump(wf_copy, default_flow_style=False, allow_unicode=True, sort_keys=False)


def serialize_workflow_text(lsg: dict, wf_id: str) -> str:
    """Compact human-readable text of one workflow's states/transitions.

    Used by the scenario-scan prompt (phase2_scenario.j2 ``workflow_lsg``).
    """
    wf = extract_workflow(lsg, wf_id)
    if wf is None:
        return f"(workflow {wf_id} not found in LSG)"
    lines = [f"workflow: {wf_id}"]
    for st in wf.get("states", []):
        lines.append(f"  state: {st['id']} [{st.get('category', '')}]")
        for tr in st.get("transitions", []):
            lines.append(f"    → {tr['guard']} → {tr['next_state']}")
    return "\n".join(lines)


def annotate_transitions(
    lsg: dict, wf_id: str, guards: list[str], scenario_id: str,
) -> None:
    """Tag matching transitions in *lsg* with *scenario_id* (in-place, idempotent)."""
    for wf in lsg.get("workflows", []):
        if wf.get("id") != wf_id:
            continue
        for st in wf.get("states", []):
            for tr in st.get("transitions", []):
                if tr.get("guard") in guards:
                    existing = tr.get("scenario_ids", [])
                    if scenario_id not in existing:
                        tr["scenario_ids"] = existing + [scenario_id]


def next_state_cat(state_id: str) -> str:
    """Trailing token of a dotted state id (e.g. ``initial.wait`` → ``wait``)."""
    return str(state_id).split(".")[-1]
