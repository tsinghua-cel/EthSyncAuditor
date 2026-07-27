"""Shared retrieval accumulation.

Collapses the per-result collection logic duplicated across the four retrieval
loops (phase1_sub, phase2_sub, phase2_scenario, phase3_verify): dedup by
(file, start_line), truncate to a char budget, assign a stable evidence id.

``SnippetAccumulator`` is the single primitive. Each agent keeps its own
query-generation loop (they differ in scope/tier logic) but delegates the
dedup/truncate/id step here.

Truncation modes (F1):
  - ``snippet_chars=None`` (default): budget-aware — keep the full function
    body when it fits the remaining budget, else head-cut to what remains.
  - ``snippet_chars=int``: legacy fixed cap (600/400/500/320).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SnippetAccumulator:
    """Accumulate retrieved snippets with dedup, char budget, and stable ids."""

    def __init__(
        self,
        *,
        id_prefix: str,
        max_snippets: int = 40,
        max_total_chars: int = 32_000,
        snippet_chars: int | None = None,
        id_format: str = "S{n}",
        dedup_key: str = "file_start",
    ) -> None:
        self.id_prefix = id_prefix
        self.max_snippets = max_snippets
        self.max_total_chars = max_total_chars
        self.snippet_chars = snippet_chars
        self.id_format = id_format
        self.dedup_key = dedup_key
        self.snippets: list[dict] = []
        self.seen: set[tuple] = set()
        self.total_chars = 0

    @property
    def full(self) -> bool:
        return (
            len(self.snippets) >= self.max_snippets
            or self.total_chars >= self.max_total_chars
        )

    def _key(self, r: Any) -> tuple:
        m = r.metadata
        if self.dedup_key == "file_start_end":
            return (m.get("file_path", ""), m.get("start_line", 0), m.get("end_line", 0))
        return (m.get("file_path", ""), m.get("start_line", 0))

    def add(self, r: Any) -> bool:
        """Add one :class:`tools.search.SearchResult`. Returns True if added."""
        if self.full:
            return False
        m = r.metadata
        key = self._key(r)
        if key in self.seen:
            return False
        body = r.content or ""
        code = self._truncate(body)
        if not code:
            return False
        self.seen.add(key)
        ev_id = f"{self.id_prefix}::{self.id_format.format(n=len(self.snippets) + 1)}"
        self.snippets.append({
            "id": ev_id,
            "file": m.get("file_path", ""),
            "function": m.get("function_name", ""),
            "start_line": m.get("start_line", 0),
            "end_line": m.get("end_line", 0),
            "code": code,
        })
        self.total_chars += len(code)
        return True

    def _truncate(self, body: str) -> str:
        budget_left = self.max_total_chars - self.total_chars
        if budget_left <= 0:
            return ""
        if self.snippet_chars is None:
            # Budget-aware full body.
            return body if len(body) <= budget_left else body[:budget_left]
        return body[: self.snippet_chars]

    def result(self) -> list[dict]:
        return self.snippets[: self.max_snippets]
