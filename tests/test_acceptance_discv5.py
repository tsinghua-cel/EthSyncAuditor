"""Acceptance test: the parameter track must surface the discv5 UDP receive
rate-limiting divergence documented in ``discv5.md``.

Ground truth (discv5.md):
  - Teku       : total 250000 bytes/sec, excess dropped
  - Prysm      : none
  - Lighthouse : per-IP two-pass (initial 9/s + total 10/s; final 8/s/node),
                 default OFF, needs CLI flag
  - Grandine   : same as Lighthouse (shared code origin)
  - Lodestar   : defines Lighthouse's rate limiter but never uses it

This is a networking-layer parameter/structural divergence that the
FSM-only, workflow-scoped model structurally cannot express.
"""

from __future__ import annotations

from agents.param_main_agent import compare_domain


def _client_aspects() -> dict:
    two_pass = ("per-ip two-pass: initial 9/s + total 10/s; final 8/s per node")
    return {
        "teku": {"discv5::udp_rate_limit": {
            "client": "teku", "value": "250000 bytes/sec total", "value_type": "literal"}},
        "prysm": {"discv5::udp_rate_limit": {
            "client": "prysm", "value": "absent", "value_type": "absent"}},
        "lighthouse": {"discv5::udp_rate_limit": {
            "client": "lighthouse", "value": two_pass, "value_type": "literal"}},
        "grandine": {"discv5::udp_rate_limit": {
            "client": "grandine", "value": two_pass, "value_type": "literal"}},
        "lodestar": {"discv5::udp_rate_limit": {
            "client": "lodestar", "value": "RateLimiterBuilder defined but never used",
            "value_type": "defined_but_unused"}},
    }


def _behavior_aspects() -> dict:
    return {"discv5::udp_rate_limit": {
        "id": "discv5::udp_rate_limit", "domain_id": "discv5",
        "name": "UDP receive rate limiting", "aspect_type": "parameter", "unit": "policy"}}


def _rate_limit_div():
    divs = compare_domain("discv5", _client_aspects(), _behavior_aspects())
    assert any(d["aspect_id"] == "discv5::udp_rate_limit" for d in divs), \
        "expected a discv5 udp_rate_limit divergence, got none"
    return next(d for d in divs if d["aspect_id"] == "discv5::udp_rate_limit")


def test_four_distinct_value_groups():
    d = _rate_limit_div()
    groups = d["value_groups"]
    assert len(groups) == 4, f"expected 4 groups, got {groups}"

    # Teku alone on the 250000 total-bytes policy
    teku_g = [g for g, cs in groups.items() if "teku" in cs]
    assert teku_g and teku_g[0].startswith("250000")

    # Prysm folded into the canonical 'absent' bucket
    prysm_g = [g for g, cs in groups.items() if "prysm" in cs]
    assert prysm_g == ["absent"]

    # Lighthouse + Grandine share the per-IP two-pass group
    lh_g = [g for g, cs in groups.items() if "lighthouse" in cs]
    assert lh_g, "lighthouse missing from groups"
    assert groups[lh_g[0]] == ["grandine", "lighthouse"]

    # Lodestar in the defined-but-unused bucket
    lodestar_g = [g for g, cs in groups.items() if "lodestar" in cs]
    assert lodestar_g == ["defined_but_unused"]


def test_all_five_clients_involved_and_critical():
    d = _rate_limit_div()
    assert set(d["involved_clients"]) == {
        "teku", "prysm", "lighthouse", "grandine", "lodestar"}
    # 'rate'/'limit'/'ban' are security-sensitive keywords → CRITICAL
    assert d["severity"] == "CRITICAL"


def test_divergence_type_is_presence_split():
    # 'absent' (prysm) and 'defined_but_unused' (lodestar) are present → the
    # divergence is partly about presence, not just value split.
    d = _rate_limit_div()
    assert d["divergence_type"] == "presence_split"
