"""Shared evidence-grounding helpers.

Collapses the duplicated ``_norm_path`` (×4) and the evidence-pool grounding
logic (×4 variants across phase1_sub, phase2_sub, phase2_scenario, phase3).

Grounding = reject LLM-emitted evidence whose file is not backed by a real
retrieved snippet (with a basename fallback), optionally also accepting
on-disk existence under ``code/{client}/`` for error/boundary transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import CODE_BASE_PATH


def norm_path(p: str) -> str:
    """Normalise a path for case/separator-insensitive comparison."""
    return p.replace("\\", "/").strip().lower()


@dataclass
class EvidencePool:
    """Lookup sets built from retrieved snippets."""

    full_paths: set[str] = field(default_factory=set)   # normalised rel paths
    basenames: set[str] = field(default_factory=set)    # file basenames
    path_map: dict[str, str] = field(default_factory=dict)  # path/basename → snippet id


def build_evidence_pool(snippets: list[dict], client_name: str = "") -> EvidencePool:
    """Build an :class:`EvidencePool` from retrieved snippet dicts.

    Each snippet is expected to carry ``file`` (and optionally ``id``).
    """
    pool = EvidencePool()
    for s in snippets:
        fp = norm_path(s.get("file", ""))
        if not fp:
            continue
        pool.full_paths.add(fp)
        bn = fp.rsplit("/", 1)[-1]
        pool.basenames.add(bn)
        sid = s.get("id")
        if sid:
            pool.path_map[fp] = sid
            pool.path_map[bn] = sid
    return pool


def is_real_evidence(
    ev: dict | None,
    pool: EvidencePool,
    client_name: str = "",
    *,
    strict: bool = True,
) -> bool:
    """True if *ev*'s file is in *pool* (basename fallback) or — when not
    ``strict`` — exists on disk under ``code/{client_name}/``."""
    if not ev or not ev.get("file"):
        return False
    fp = norm_path(ev["file"])
    bn = fp.rsplit("/", 1)[-1]
    if fp in pool.full_paths or bn in pool.basenames:
        return True
    if strict:
        return False
    # Relaxed: accept real on-disk sibling files (error/boundary handlers).
    disk_path = CODE_BASE_PATH / client_name / ev["file"]
    return disk_path.exists()


def ground_workflow(
    wf: dict, snippets: list[dict], client_name: str,
) -> tuple[dict, int, int]:
    """Replace hallucinated evidence with None; keep real evidence.

    Per transition_type:
      - ``error``/``boundary``: accepted if the file exists on disk under
        ``code/{client}/`` even when not retrieved (relaxed).
      - ``normal`` (default): must be in the retrieved pool (strict).

    Returns ``(grounded_workflow, kept_count, cleared_count)``.
    """
    pool = build_evidence_pool(snippets, client_name)
    kept = cleared = 0
    for st in wf.get("states", []):
        for tr in st.get("transitions", []):
            ev = tr.get("evidence")
            if ev is None:
                continue
            ttype = tr.get("transition_type", "normal")
            relaxed = ttype in ("error", "boundary")
            if is_real_evidence(ev, pool, client_name, strict=not relaxed):
                kept += 1
            else:
                tr["evidence"] = None
                cleared += 1
    return wf, kept, cleared


def validate_vocab_entries(
    entries: list[dict], allowed_files: set[str], allowed_basenames: set[str],
) -> list[dict]:
    """Keep only vocab entries whose ``evidence_file`` resolves to a real file.

    (Phase-1 style: entries carry ``evidence_file`` rather than nested
    ``evidence``.) *allowed_files* / *allowed_basenames* are normalised sets.
    """
    kept: list[dict] = []
    for e in entries:
        ev_file = norm_path(e.get("evidence_file") or "")
        if not ev_file:
            continue
        base = ev_file.rsplit("/", 1)[-1]
        if ev_file in allowed_files or base in allowed_basenames:
            kept.append(e)
    return kept
