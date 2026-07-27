"""Acceptance test: the parameter track must surface the peer-scoring
divergence documented in ``beacon_peer.md``.

Ground truth (beacon_peer.md):
  - Prysm       : 4-dimension weighted sum (gossip 0.4 / bad 0.3 / status 0.3 / block 0.0)
  - Teku        : three independent scores (gossipsub, SubnetScorer, Reputation)
  - Lighthouse  : composite = reputation + gossipsub*0.0011875; ban@-50, disconnect@-20,
                  ban cooldown 12h (43200s), trusted peer = INFINITY
  - Grandine    : identical to Lighthouse (shared code origin)
  - Lodestar    : isomorphic port of Lighthouse with 4 constant diffs:
                  ban cooldown 30min (1800s) vs 12h; trusted peer 100 vs INFINITY;
                  (plus Fatal add(-200) clamp vs set(-100); reconnection cooldown)

These are algorithm-shape + constant divergences in the p2p/peerdb layer that
the FSM-only model cannot express.
"""

from __future__ import annotations

from agents.param_main_agent import compare_domain


def _algo_aspects() -> dict:
    composite = "composite = reputation + gossipsub*0.0011875"
    return {
        "prysm": {"peer_scoring::algorithm_shape": {
            "client": "prysm",
            "value": "4-dimension weighted sum (gossip 0.4, bad 0.3, status 0.3)",
            "value_type": "structural"}},
        "teku": {"peer_scoring::algorithm_shape": {
            "client": "teku",
            "value": "three independent scores (gossipsub, subnet, reputation)",
            "value_type": "structural"}},
        "lighthouse": {"peer_scoring::algorithm_shape": {
            "client": "lighthouse", "value": composite, "value_type": "structural"}},
        "grandine": {"peer_scoring::algorithm_shape": {
            "client": "grandine", "value": composite, "value_type": "structural"}},
        "lodestar": {"peer_scoring::algorithm_shape": {
            "client": "lodestar",
            "value": "composite like lighthouse but with 4 constant diffs",
            "value_type": "structural"}},
    }


def _algo_meta() -> dict:
    return {"peer_scoring::algorithm_shape": {
        "id": "peer_scoring::algorithm_shape", "domain_id": "peer_scoring",
        "name": "peer scoring algorithm", "aspect_type": "algorithm_shape"}}


def test_algorithm_shape_four_groups():
    divs = compare_domain("peer_scoring", _algo_aspects(), _algo_meta())
    d = next(x for x in divs if x["aspect_id"] == "peer_scoring::algorithm_shape")
    assert len(d["value_groups"]) == 4, d["value_groups"]
    assert d["divergence_type"] == "algorithm_shape"
    assert set(d["involved_clients"]) == {
        "prysm", "teku", "lighthouse", "grandine", "lodestar"}
    # Lighthouse + Grandine share the composite group
    for g, cs in d["value_groups"].items():
        if "lighthouse" in cs:
            assert cs == ["grandine", "lighthouse"]


def test_ban_cooldown_lodestar_is_the_deviator():
    ca = {
        "prysm": {"peer_scoring::ban_cooldown_seconds": {
            "client": "prysm", "value": "no time-based ban decay", "value_type": "literal"}},
        "teku": {"peer_scoring::ban_cooldown_seconds": {
            "client": "teku", "value": "43200", "value_type": "literal"}},
        "lighthouse": {"peer_scoring::ban_cooldown_seconds": {
            "client": "lighthouse", "value": "43200", "value_type": "literal"}},
        "grandine": {"peer_scoring::ban_cooldown_seconds": {
            "client": "grandine", "value": "43200", "value_type": "literal"}},
        "lodestar": {"peer_scoring::ban_cooldown_seconds": {
            "client": "lodestar", "value": "1800", "value_type": "literal"}},
    }
    ba = {"peer_scoring::ban_cooldown_seconds": {
        "id": "peer_scoring::ban_cooldown_seconds", "domain_id": "peer_scoring",
        "name": "ban cooldown seconds", "aspect_type": "constant", "unit": "seconds"}}
    divs = compare_domain("peer_scoring", ca, ba)
    d = next(x for x in divs if x["aspect_id"] == "peer_scoring::ban_cooldown_seconds")
    groups = d["value_groups"]
    assert groups["1800"] == ["lodestar"]
    assert set(groups["43200"]) == {"teku", "lighthouse", "grandine"}
    assert "lodestar" in d["deviating_clients"]
    assert d["severity"] == "CRITICAL"  # 'ban' keyword


def test_trusted_peer_value_lodestar_deviates():
    ca = {
        "lighthouse": {"peer_scoring::trusted_peer_value": {
            "client": "lighthouse", "value": "INFINITY", "value_type": "literal"}},
        "grandine": {"peer_scoring::trusted_peer_value": {
            "client": "grandine", "value": "INFINITY", "value_type": "literal"}},
        "lodestar": {"peer_scoring::trusted_peer_value": {
            "client": "lodestar", "value": "100", "value_type": "literal"}},
    }
    ba = {"peer_scoring::trusted_peer_value": {
        "id": "peer_scoring::trusted_peer_value", "domain_id": "peer_scoring",
        "name": "trusted peer score value", "aspect_type": "constant"}}
    divs = compare_domain("peer_scoring", ca, ba)
    d = next(x for x in divs if x["aspect_id"] == "peer_scoring::trusted_peer_value")
    assert d["value_groups"]["100"] == ["lodestar"]
    assert set(d["value_groups"]["infinity"]) == {"lighthouse", "grandine"}
    assert d["deviating_clients"] == ["lodestar"]
