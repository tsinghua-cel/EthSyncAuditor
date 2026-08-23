"""Shared structured-output LLM invocation.

Collapses the ``chain = llm.with_structured_output(Schema); invoke_with_retry(
chain, prompt, label=..., callbacks=...)`` idiom repeated across every agent.
"""

from __future__ import annotations

from typing import Any

from utils import invoke_with_retry


def invoke_structured(
    llm: Any,
    schema: Any,
    prompt: str,
    *,
    label: str,
    callbacks: Any = None,
    max_retries: int = 3,
) -> Any:
    """Bind *schema* for structured output, then invoke with retry/backoff.

    Returns the validated pydantic *schema* instance.
    """
    chain = llm.with_structured_output(schema)
    return invoke_with_retry(
        chain, prompt, label=label, callbacks=callbacks, max_retries=max_retries,
    )
