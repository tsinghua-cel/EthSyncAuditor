"""Shared helpers used by agents, file_io and state."""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def safe_serialize(obj: Any) -> Any:
    """Recursively convert *obj* into a JSON/YAML-friendly structure.

    Handles pydantic models, dataclasses, sets/tuples, Path objects and
    falls back to ``str(obj)`` for unknown types.
    """
    from pathlib import Path

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, BaseModel):
        return safe_serialize(obj.model_dump())
    if isinstance(obj, dict):
        return {str(k): safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [safe_serialize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dict__"):
        try:
            return safe_serialize(vars(obj))
        except Exception:
            return str(obj)
    return str(obj)


def invoke_with_retry(chain: Any, prompt: Any, *,
                      label: str = "llm",
                      callbacks: list[Any] | None = None,
                      max_retries: int = 3,
                      base_delay: float = 2.0) -> Any:
    """Invoke ``chain.invoke(prompt, …)`` with exponential backoff."""
    last_exc: Exception | None = None
    cfg = {"callbacks": callbacks} if callbacks else None
    for attempt in range(1, max_retries + 1):
        try:
            return chain.invoke(prompt, config=cfg) if cfg else chain.invoke(prompt)
        except Exception as exc:  # broad: LLM SDKs raise wildly varied types
            last_exc = exc
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("[%s] attempt %d/%d failed: %s — retrying in %.1fs",
                           label, attempt, max_retries, exc, delay)
            time.sleep(delay)
    logger.error("[%s] giving up after %d attempts", label, max_retries)
    assert last_exc is not None
    raise last_exc


def summarize_vocab_for_prompt(guards: list[dict], actions: list[dict],
                               max_full_entries: int = 80) -> dict[str, Any]:
    """Build the vocabulary view consumed by ``phase2_sub.j2``.

    Returned shape::

        {
          "total_guards":    int,
          "total_actions":   int,
          "guard_summary":   {category: [name, ...]},   # all entries
          "action_summary":  {category: [name, ...]},   # all entries
          "guard_details":   [{name, category, description}, ...],
          "action_details":  [{name, category, description}, ...],
        }

    When ``len(guards) + len(actions) > max_full_entries`` the
    ``*_details`` lists are truncated (head-cut, half budget each).
    The summary maps remain complete so the LLM still sees every name.
    """
    def _group(items: list[dict]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in items:
            cat = (e.get("category") or "uncategorized") if isinstance(e, dict) else "uncategorized"
            name = e.get("name", "") if isinstance(e, dict) else str(e)
            if name:
                out.setdefault(cat, []).append(name)
        return out

    def _detail(items: list[dict]) -> list[dict]:
        return [
            {"name": e.get("name", ""),
             "category": e.get("category", "uncategorized"),
             "description": e.get("description", "")}
            for e in items if isinstance(e, dict) and e.get("name")
        ]

    g_details = _detail(guards)
    a_details = _detail(actions)
    if len(g_details) + len(a_details) > max_full_entries:
        keep = max(1, max_full_entries // 2)
        g_details = g_details[:keep]
        a_details = a_details[:keep]

    return {
        "total_guards": len(guards),
        "total_actions": len(actions),
        "guard_summary": _group(guards),
        "action_summary": _group(actions),
        "guard_details": g_details,
        "action_details": a_details,
    }


def compute_lsg_sparsity(client_lsgs: dict[str, dict],
                         min_states: int = 3,
                         min_transitions: int = 4) -> list[dict]:
    """Return a hint per (client, workflow) when the LSG looks sparse.

    A workflow is flagged when it has fewer than *min_states* states or
    fewer than *min_transitions* transitions.
    """
    hints: list[dict] = []
    for client, lsg in client_lsgs.items():
        if not isinstance(lsg, dict):
            continue
        for wf in lsg.get("workflows", []) or []:
            wf_id = wf.get("id") or wf.get("name") or ""
            states = wf.get("states", []) or []
            n_states = len(states)
            n_trans = sum(len(s.get("transitions", []) or []) for s in states)
            if n_states < min_states or n_trans < min_transitions:
                hints.append({
                    "client": client,
                    "workflow_id": wf_id,
                    "states": n_states,
                    "transitions": n_trans,
                })
    return hints

