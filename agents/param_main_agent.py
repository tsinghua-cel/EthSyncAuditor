"""Parameter / behavior-divergence Main Agent (subsystem domains).

Deterministic cross-client comparison of the parameter/behavior values that
``param_sub_agent`` extracted. For each :class:`BehaviorAspect` it groups
clients by their (normalised) value and emits a :class:`ParameterDivergence`
when the clients split. No LLM call — the comparison is exact-match grouping
plus severity heuristics, so it is fully unit-testable without a model.

This is the layer that surfaces differences the FSM track structurally cannot
express: discv5 rate-limit constants, peer-scoring algorithm shapes and
weights, ban thresholds / cooldowns, etc.
"""

from __future__ import annotations

import logging
from typing import Any

from config import get_domain
from state import BehaviorAspect, ParameterDivergence, ParameterValue

logger = logging.getLogger(__name__)

# Keywords that mark an aspect security-sensitive → severity CRITICAL.
_CRIT_KEYWORDS = (
    "ban", "slashing", "invalid", "optimistic", "rate", "limit",
    "threshold", "cascade", "ddos", "flood", "protection",
)

_ABSENT_TOKENS = {"", "absent", "none", "n/a", "na", "null", "no", "not present", "missing"}


def _normalize_value(value: str, value_type: str) -> str:
    """Normalise a value string for grouping (case/whitespace-insensitive).

    ``absent`` and ``defined_but_unused`` are folded into canonical tokens so a
    client that lacks a feature groups with others lacking it (and apart from
    clients that implement it differently).
    """
    v = " ".join((value or "").strip().lower().split())
    if value_type == "defined_but_unused" or v == "defined_but_unused":
        return "defined_but_unused"
    if value_type == "absent" or v in _ABSENT_TOKENS:
        return "absent"
    return v


def _deviating_clients(value_groups: dict[str, list[str]]) -> list[str]:
    """Clients NOT in the unique largest group.

    When several groups tie for largest (no clear majority), every client is
    considered deviating — there is no reference behaviour to deviate *from*.
    """
    if not value_groups:
        return []
    sizes = [len(c) for c in value_groups.values()]
    max_size = max(sizes)
    majors = [grp for grp in value_groups.values() if len(grp) == max_size]
    if len(majors) > 1:
        return sorted({c for grp in value_groups.values() for c in grp})
    majority = majors[0]
    return sorted({c for grp in value_groups.values() for c in grp if grp is not majority})


def _severity(name: str, value_groups: dict[str, list[str]], aspect_type: str) -> str:
    blob = (name + " " + " ".join(value_groups)).lower()
    if any(k in blob for k in _CRIT_KEYWORDS):
        return "CRITICAL"
    return "MAJOR"


def _divergence_type(aspect_type: str, groups: dict[str, list[str]]) -> str:
    if aspect_type == "algorithm_shape":
        return "algorithm_shape"
    if {"absent", "defined_but_unused"} & set(groups):
        return "presence_split"
    return "value_split"


def _describe(name: str, groups: dict[str, list[str]]) -> str:
    body = "; ".join(f"{','.join(cs)} → '{k}'" for k, cs in groups.items())
    return f"{len(groups)} groups for '{name}': {body}"


def _coerce_pv(pv: Any) -> ParameterValue:
    if isinstance(pv, ParameterValue):
        return pv
    if isinstance(pv, dict):
        return ParameterValue(**pv)
    return ParameterValue(client="", value=str(pv))


def _aspect_meta(behavior_aspects: dict[str, Any], aid: str) -> tuple[str, str, str]:
    """Return (name, aspect_type, unit) for *aid* from the merged aspects map."""
    asp = (behavior_aspects or {}).get(aid)
    if asp is None:
        slug = aid.split("::", 1)[-1]
        return slug, "parameter", ""
    if isinstance(asp, BehaviorAspect):
        return asp.name or aid, asp.aspect_type or "parameter", asp.unit or ""
    if isinstance(asp, dict):
        return (asp.get("name") or aid, asp.get("aspect_type") or "parameter",
                asp.get("unit") or "")
    return aid, "parameter", ""


def compare_domain(
    domain_id: str,
    client_aspects: dict[str, dict[str, Any]],
    behavior_aspects: dict[str, Any] | None = None,
) -> list[dict]:
    """Pure deterministic comparison for one subsystem domain.

    Returns a list of :class:`ParameterDivergence` dicts (one per aspect where
    ≥2 clients were extracted and they do not all agree).
    """
    domain = get_domain(domain_id)
    if domain is None:
        logger.warning("[param_main] unknown domain %s", domain_id)
        return []
    prefix = f"{domain_id}::"

    per_aspect: dict[str, dict[str, ParameterValue]] = {}
    for client, asp_map in (client_aspects or {}).items():
        if not isinstance(asp_map, dict):
            continue
        for aid, pv in asp_map.items():
            if not aid.startswith(prefix):
                continue
            per_aspect.setdefault(aid, {})[client] = _coerce_pv(pv)

    out: list[dict] = []
    for aid, per_client in per_aspect.items():
        if len(per_client) < 2:
            continue
        groups: dict[str, list[str]] = {}
        for c, pv in per_client.items():
            key = _normalize_value(pv.value, pv.value_type)
            groups.setdefault(key, []).append(c)
        if len(groups) <= 1:
            continue  # all extracted clients agree

        name, aspect_type, _unit = _aspect_meta(behavior_aspects or {}, aid)
        div_type = _divergence_type(aspect_type, groups)
        deviating = _deviating_clients(groups)
        involved = sorted(per_client.keys())
        pcv = {c: per_client[c].model_dump() for c in involved}

        out.append(ParameterDivergence(
            aspect_id=aid,
            domain_id=domain_id,
            name=name,
            divergence_type=div_type,
            description=_describe(name, groups),
            severity=_severity(name, groups, aspect_type),
            involved_clients=involved,
            deviating_clients=deviating,
            per_client_values=pcv,
            value_groups={k: sorted(v) for k, v in groups.items()},
        ).model_dump())
    return out


def build_param_main_agent(llm: Any = None, callbacks: Any = None):
    """Build the parameter-track main agent (deterministic; *llm* unused in v1)."""

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        domain_id = state.get("current_domain", "")
        if not domain_id:
            return {}
        divs = compare_domain(
            domain_id,
            state.get("client_aspects", {}),
            state.get("behavior_aspects", {}),
        )
        logger.info(
            "[param_main] domain=%s aspects_compared → %d divergences",
            domain_id, len(divs),
        )
        return {
            "parameter_divergences": divs,
            "domain_diff_reports": {domain_id: {"parameter_divergences": divs}},
        }

    return _run
