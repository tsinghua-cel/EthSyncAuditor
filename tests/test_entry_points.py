"""Tests for call-graph entry-point detection.

Covers the three additive signals used to seed workflow entry points:
function-name keyword, qualified-name keyword (receiver/class qualified), and
workflow-specific file-path markers — plus test-symbol skipping and dedup.
"""

from __future__ import annotations

from tools.preprocessor import _build_callgraph, SymbolInfo


def _sym(fn: str, qn: str, file: str) -> SymbolInfo:
    return SymbolInfo(
        file=file, function_name=fn, qualified_name=qn,
        start_line=1, end_line=2, calls=[],
    )


def test_entry_point_signals_and_filters():
    syms = [
        _sym("RangeSync", "RangeSync", "beacon-chain/sync/initial-sync/service.go"),
        _sym("run", "(*Service).run", "beacon-chain/sync/initial-sync/round_robin.go"),
        _sym("receiveBlock", "(*Service).receiveBlock", "beacon-chain/sync/gossip.go"),
        _sym("helperUnrelated", "helperUnrelated", "shared/math/util.go"),
        _sym("TestRangeSync", "TestRangeSync", "beacon-chain/sync/initial-sync/x_test.go"),
        _sym("RangeSync", "RangeSync", "beacon-chain/sync/initial-sync/dup.go"),
    ]
    ep = _build_callgraph("prysm", syms).entry_points

    # Signal 1: function-name keyword.
    assert "RangeSync" in ep["initial_sync"]
    # Signal 3: path-only entry (generic name in a workflow-specific dir).
    assert "(*Service).run" in ep["initial_sync"]
    # Signal 2: qualified-name keyword (Go receiver).
    assert "(*Service).receiveBlock" in ep["regular_sync"]
    # Unrelated symbol is never an entry point.
    assert all(
        "helperUnrelated" not in entries for entries in ep.values()
    )
    # Test symbols are skipped everywhere.
    assert all(
        "Test" not in e for entries in ep.values() for e in entries
    )
    # Dedup by qualified_name.
    assert ep["initial_sync"].count("RangeSync") == 1


def test_overrides_take_precedence():
    syms = [_sym("RangeSync", "RangeSync", "beacon-chain/sync/initial-sync/s.go")]
    import tools.preprocessor as pp

    saved = pp.ENTRY_POINT_OVERRIDES
    try:
        pp.ENTRY_POINT_OVERRIDES = {
            "prysm": {"initial_sync": ["MyExplicitEntry"]}
        }
        ep = _build_callgraph("prysm", syms).entry_points
        assert ep["initial_sync"] == ["MyExplicitEntry"]
    finally:
        pp.ENTRY_POINT_OVERRIDES = saved
