"""Shared pytest fixtures for EthSyncAuditor.

Helpers for building ParameterValue / BehaviorAspect without boilerplate, used
by the parameter-track acceptance tests. The canonical LLM mock
(``FakeLLM``) mirrors the schema-dispatch pattern from test_phase1_grounding.
"""

from __future__ import annotations

from typing import Any

from state import BehaviorAspect, Evidence, ParameterValue


def make_pv(
    client: str,
    value: str,
    *,
    value_type: str = "literal",
    file: str = "",
    function: str = "",
    lines: list[int] | None = None,
    notes: str = "",
) -> ParameterValue:
    evidence = Evidence(file=file, function=function, lines=lines or []) if file else None
    return ParameterValue(
        client=client, value=value, value_type=value_type,
        evidence=evidence, notes=notes,
    )


def make_aspect(
    aid: str, domain: str, name: str, *, aspect_type: str = "parameter", unit: str = "",
) -> BehaviorAspect:
    return BehaviorAspect(
        id=aid, domain_id=domain, name=name, aspect_type=aspect_type, unit=unit,
    )


class FakeChain:
    """Returns a stashed object on .invoke(), ignoring the prompt."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def invoke(self, prompt: Any, **kwargs: Any) -> Any:  # noqa: D401
        return self._obj


class FakeLLM:
    """LLM mock that dispatches ``with_structured_output(schema)`` to a stashed
    result keyed by schema class — the same two-call pattern the real agents use.
    """

    def __init__(self, by_schema: dict[type, Any]) -> None:
        self._by_schema = by_schema

    def with_structured_output(self, schema: Any, **kwargs: Any) -> FakeChain:
        obj = self._by_schema.get(schema)
        if obj is None:
            raise AssertionError(f"FakeLLM has no stashed result for {schema}")
        return FakeChain(obj)
