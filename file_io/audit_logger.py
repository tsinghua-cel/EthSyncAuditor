"""Audit logger via LangChain callbacks.

Records every LLM invocation's prompt, chain-of-thought and raw response
as structured JSON files under ``./output/audit_logs/``.

Each agent node creates its own :class:`AuditLogCallback` instance with the
correct ``phase``, ``iteration``, and ``agent_type`` metadata so that log
files are properly attributed — e.g.
``audit_phase2_iter3_phase2_sub_lighthouse_20260402T120000_000000.json``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

import config
from utils import safe_serialize

logger = logging.getLogger(__name__)


def _extract_response_text(response: Any) -> str | None:
    """Best-effort extraction of the response text from an LLMResult.

    LangChain wraps responses in ``LLMResult.generations`` which is a nested
    list of ``Generation`` / ``ChatGeneration`` objects.  This helper returns
    the first non-empty text (or message content) it can find.
    """
    try:
        # response may be an LLMResult object or an already-serialized dict
        gens = getattr(response, "generations", None)
        if gens is None and isinstance(response, dict):
            gens = response.get("generations")
        if not gens:
            return None
        first_gen = gens[0][0] if gens and gens[0] else None
        if first_gen is None:
            return None
        # ChatGeneration stores content in .message.content
        msg = getattr(first_gen, "message", None)
        if msg is not None:
            content = getattr(msg, "content", "")
            if content:
                return str(content)
        # Plain Generation stores content in .text
        text = getattr(first_gen, "text", None)
        if text is None and isinstance(first_gen, dict):
            text = first_gen.get("text", "")
            if not text:
                msg_dict = first_gen.get("message", {})
                if isinstance(msg_dict, dict):
                    text = msg_dict.get("kwargs", {}).get("content", "")
        return str(text) if text else None
    except Exception:
        return None


class AuditLogCallback(BaseCallbackHandler):
    """LangChain-compatible callback handler for audit logging.

    Captures both **text-LLM** calls (``on_llm_start``) and **chat-model**
    calls (``on_chat_model_start``), so it works with ``ChatAnthropic``,
    ``ChatGoogleGenerativeAI``, and embedding models alike.

    Files are written as:
        ``audit_phase<P>_iter<N>_<agent_type>_<timestamp>.json``
    """

    def __init__(self, phase: int = 0, iteration: int = 0, agent_type: str = "unknown"):
        self.phase = phase
        self.iteration = iteration
        self.agent_type = agent_type
        self._paths: list[str] = []

    # ── Text-LLM hooks ──────────────────────────────────────────────────

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called before a text-LLM call (e.g. embeddings, completions)."""
        self._save_event("llm_start", {
            "model_type": "text_llm",
            "serialized": safe_serialize(serialized),
            "prompts": prompts,
        })

    # ── Chat-model hooks ────────────────────────────────────────────────

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called before a chat-model call (ChatAnthropic, ChatGemini, …).

        Chat models fire this instead of ``on_llm_start``.
        """
        # Serialize messages to plain strings for JSON storage
        prompt_texts: list[str] = []
        for msg_list in messages:
            for msg in msg_list:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", str(msg))
                # Truncate very large prompts to keep log files manageable
                if isinstance(content, str) and len(content) > 50_000:
                    content = content[:50_000] + f"\n... [truncated, total {len(getattr(msg, 'content', ''))} chars]"
                prompt_texts.append(f"[{role}] {content}")

        self._save_event("llm_start", {
            "model_type": "chat_model",
            "serialized": safe_serialize(serialized),
            "prompts": prompt_texts,
        })

    # ── Common end / error hooks ────────────────────────────────────────

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called after an LLM/chat-model call completes."""
        response_text = _extract_response_text(response)
        payload: dict[str, Any] = {
            "response": safe_serialize(response),
        }
        if response_text is not None:
            # Store extracted text separately for easy inspection
            payload["response_text_preview"] = (
                response_text[:2000] + "…"
                if len(response_text) > 2000
                else response_text
            )
            payload["response_text_length"] = len(response_text)
        self._save_event("llm_end", payload)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Called on LLM error."""
        self._save_event("llm_error", {
            "error": str(error),
            "error_type": type(error).__name__,
        })

    # ── Internal ────────────────────────────────────────────────────────

    def _save_event(self, event_type: str, payload: dict[str, Any]) -> None:
        config.AUDIT_LOG_PATH.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        filename = (
            f"audit_phase{self.phase}_iter{self.iteration}"
            f"_{self.agent_type}_{ts}.json"
        )
        path = config.AUDIT_LOG_PATH / filename

        record = {
            "event_type": event_type,
            "phase": self.phase,
            "iteration": self.iteration,
            "agent_type": self.agent_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False, default=str)

        self._paths.append(str(path))
        logger.debug("[audit_logger] %s → %s", event_type, path)

    @property
    def paths(self) -> list[str]:
        return list(self._paths)


# ── Evidence-audit JSONL writer (audit-review remediation) ───────────────


def write_evidence_audit(state: dict[str, Any]) -> str | None:
    """Flush ``state["evidence_audit"]`` to a JSONL file under ``audit_logs/``.

    Each record corresponds to one finding × client validation result and is
    written exactly as produced by ``tools.evidence_validator`` /
    ``graph.evidence_gate_node``. Returns the file path or ``None`` if the
    audit channel is empty.
    """
    records = state.get("evidence_audit") or []
    if not records:
        return None

    config.AUDIT_LOG_PATH.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = config.AUDIT_LOG_PATH / f"evidence_audit_{ts}.jsonl"

    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str))
            f.write("\n")

    logger.info("[write_evidence_audit] → %s (%d records)", path, len(records))
    return str(path)

