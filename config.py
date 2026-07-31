"""Centralised configuration constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Iteration limits & convergence
MAX_ITER_PHASE1: int = 10
MAX_ITER_PHASE2: int = 10
CONVERGENCE_THRESHOLD: float = 0.05

# Phase 2 stage 1: A-class delta-rate convergence
P2_A_CLASS_CONVERGENCE_THRESHOLD: float = 0.10
OSCILLATION_WINDOW: int = 3
OSCILLATION_BAND: int = 2

# Phase 2 stage 2: B-class discovery
MAX_ITER_B_CLASS: int = 5      # F9: was 3 — new-discovery capped too early
B_CLASS_STABLE_WINDOW: int = 3  # F9: was 2
B_CLASS_CHANGE_THRESHOLD: int = 0  # F9: was 1 — any change resets stability

# Phase 3: B-class verification
VERIFY_ENABLED: bool = True
VERIFY_SEARCH_TOP_K: int = 20
VERIFY_CONFIDENCE_THRESHOLD: float = 0.5

# Clients (canonical order)
CLIENT_NAMES: list[str] = [
    "prysm",
    "lighthouse",
    "grandine",
    "teku",
    "lodestar",
]

# Workflow IDs every client must implement
WORKFLOW_IDS: list[str] = [
    "initial_sync",
    "regular_sync",
    "checkpoint_sync",
    "attestation_generate",
    "block_generate",
    "aggregate",
    "execute_layer_relation",
]

# Paths
PROJECT_ROOT: Path = Path(__file__).resolve().parent
CODE_BASE_PATH: Path = PROJECT_ROOT / "code"
OUTPUT_PATH: Path = PROJECT_ROOT / "output"
PREPROCESS_PATH: Path = OUTPUT_PATH / "preprocess"
CHECKPOINT_PATH: Path = OUTPUT_PATH / "checkpoints"
ITERATIONS_PATH: Path = OUTPUT_PATH / "iterations"
AUDIT_LOG_PATH: Path = OUTPUT_PATH / "audit_logs"

# RAG hybrid weights
BM25_WEIGHT: float = 0.4
VECTOR_WEIGHT: float = 0.6

# Client → (language key, tree-sitter grammar package)
LANGUAGE_GRAMMARS: dict[str, tuple[str, str]] = {
    "prysm":      ("go",         "tree-sitter-go"),
    "lighthouse": ("rust",       "tree-sitter-rust"),
    "grandine":   ("rust",       "tree-sitter-rust"),
    "teku":       ("java",       "tree-sitter-java"),
    "lodestar":   ("typescript", "tree-sitter-typescript"),
}

# Entry-point keyword heuristics (lowercase, underscores stripped before match).
# Keep keywords specific enough to avoid colliding with cryptographic primitives.
# NOTE: entry-point matching is a substring test against each symbol's
# *function name*, lowercased with underscores stripped (see
# tools/preprocessor.py:_build_callgraph). Keywords must therefore be
# lowercase, underscore-free, and correspond to real FUNCTION-name fragments
# (struct/class/type names never match). Each list is calibrated so that
# every client (prysm/lighthouse/grandine/teku/lodestar) has at least one
# real entry point per workflow; overly generic fragments are avoided so the
# reachable set stays workflow-scoped.
ENTRY_POINT_KEYWORDS: dict[str, list[str]] = {
    "initial_sync": [
        "initialsync", "rangesync", "syncrange",
        "forwardsync", "peersync", "syncchain",
        "requestblocks", "fetchblocks", "backfill",
    ],
    "regular_sync": [
        "regularsync", "gossipsync", "gossiphandler",
        "gossipvalidator", "onblock", "importblock",
        "receiveblock", "receiveattestation", "onattestation",
        "reorg",
    ],
    "checkpoint_sync": [
        "checkpointsync", "checkpoint", "anchor",
        "weaksubjectivity", "backfill", "finalizedstate",
    ],
    "block_generate": [
        "proposeblock", "produceblock", "buildblock",
        "getpayload", "blockproposal", "proposerduty",
        "builderbid", "enginegetpayload",
    ],
    "attestation_generate": [
        "submitattestation", "createattestation", "produceattestation",
        "buildattestation", "signattestation", "attesterduty",
        "attestationduty", "attestationproduction", "slashingprotection",
    ],
    "aggregate": [
        "aggregateandproof", "isaggregator", "selectionproof",
        "aggregatorselection", "aggregationduty", "submitaggregate",
        "publishaggregate", "createaggregate",
    ],
    "execute_layer_relation": [
        "executionengine", "forkchoiceupdate", "newpayload",
        "notifynewpayload", "payloadstatus", "isoptimistic",
        "optimisticsync", "invalidpayload", "invalidateblock",
    ],
}

# Per-client entry-point overrides (client → workflow → list[fn])
ENTRY_POINT_OVERRIDES: dict[str, dict[str, list[str]]] = {}

# Workflow → file-path fragments (lowercased substrings of the client-relative
# source path). A symbol is also treated as a workflow entry point when its
# file lives in a directory that clearly belongs to the workflow, even if the
# function name does not match a keyword. This is the strongest workflow
# signal (module layout) and complements the function-name keywords above —
# it is especially useful where a client's entry functions have generic names
# (run/start/handle/process) but live in a workflow-specific package.
# Keep fragments specific to a workflow's module to avoid bleeding across
# workflows; matching is additive to ENTRY_POINT_KEYWORDS, never a replacement.
ENTRY_POINT_PATH_MARKERS: dict[str, list[str]] = {
    "initial_sync": [
        "initial-sync", "initial_sync", "range_sync", "rangesync",
        "sync/range", "forward_sync", "backfill",
    ],
    "regular_sync": [
        "gossip", "blockimporter", "block_processor",
        "networkbeaconprocessor",
    ],
    "checkpoint_sync": [
        "checkpoint", "weak_subjectivity", "weaksubjectivity",
    ],
    "block_generate": [
        "block_producer", "blockproducer", "proposer",
        "validator/client/propose", "/propose", "block_service",
        "blockproduction",
    ],
    "attestation_generate": [
        "attestation_service", "attestationservice",
        "validator/client/attest", "/attest", "slashing_protection",
        "slashingprotection",
    ],
    "aggregate": [
        "aggregat",
    ],
    "execute_layer_relation": [
        "execution_layer", "executionlayer", "execution/engine",
        "executionengine", "/engine/", "engine_api",
    ],
}

# ── Analysis domains ─────────────────────────────────────────────────────────
# Generalises "workflow" beyond the 7 consensus workflows. Subsystem domains
# (networking / p2p) are first-class analysis targets with their own entry-point
# heuristics and retrieval config. Workflow domains → FSM extraction track;
# subsystem domains → parameter / behavior-divergence track.
@dataclass
class Domain:
    id: str
    name: str
    kind: str = "workflow"            # "workflow" | "subsystem"
    description: str = ""
    entry_keywords: list[str] = field(default_factory=list)
    entry_path_markers: list[str] = field(default_factory=list)
    retrieval_queries: list[str] = field(default_factory=list)
    # Aspects the parameter extractor should specifically look for (guided
    # extraction): [{"id": slug, "desc": what to capture}].
    aspect_hints: list[dict] = field(default_factory=list)
    max_call_depth: int = 5
    max_snippets: int = 40
    max_total_chars: int = 32_000
    snippet_chars: int | None = None  # None = budget-aware full bodies
    top_k_per_query: int = 6
    extract_fsm: bool = True
    extract_parameters: bool = False


def _build_workflow_domains() -> list[Domain]:
    doms: list[Domain] = []
    for wf in WORKFLOW_IDS:
        doms.append(Domain(
            id=wf,
            name=wf.replace("_", " ").title(),
            kind="workflow",
            entry_keywords=list(ENTRY_POINT_KEYWORDS.get(wf, [])),
            entry_path_markers=list(ENTRY_POINT_PATH_MARKERS.get(wf, [])),
        ))
    return doms


# Subsystem domains — networking / p2p. Entry-point seeds are grounded in the
# file paths cited in discv5.md / beacon_peer.md. extract_parameters=True routes
# them through the parameter / behavior-divergence track (not the FSM track).
SUBSYSTEM_DOMAINS: list[Domain] = [
    Domain(
        id="discv5",
        name="discv5 Discovery",
        kind="subsystem",
        description="UDP packet handling & rate limiting in the discovery v5 layer",
        extract_fsm=False, extract_parameters=True, max_call_depth=8,
        entry_keywords=[
            "ratelimit", "ratedlim", "discv5", "readudp", "recvudp",
            "handlepacket", "onpacket", "initialpass", "finalpass", "gcra",
            "filter", "banip",
        ],
        entry_path_markers=[
            "p2p/discovery", "discv5", "discovery/", "v5wire",
            "rate_limit", "ratelimit",
        ],
        retrieval_queries=[
            "UDP packet read receive rate limit throttle total bytes per second",
            "discv5 rate limiter per-IP per-node GCRA burst total",
            "rate limiter default enabled disabled CLI flag config",
            "RateLimiterBuilder total_n_every node_n_every ip_n_every",
            "packet filter ban IP duration ban_duration filter_max_bans_per_ip",
            "discv5 discovery service recv readudp packet max size 1280",
            "rate_limiter build limit per ip node total",
        ],
        aspect_hints=[
            {"id": "udp_rate_limit_policy",
             "desc": "How incoming UDP/discv5 packets are rate-limited: total bytes/sec, per-IP, per-node, or none. State the limits and whether it is on by default or needs a CLI flag. If the limiter is defined but never called, say so."},
            {"id": "rate_limit_default_enabled",
             "desc": "Whether rate limiting is enabled by default (true) or requires a startup flag (false)."},
            {"id": "udp_max_packet_size",
             "desc": "Maximum UDP packet size accepted (e.g. 1280)."},
        ],
    ),
    Domain(
        id="peer_scoring",
        name="Peer Scoring",
        kind="subsystem",
        description="libp2p gossipsub + application-layer peer scoring & reputation",
        extract_fsm=False, extract_parameters=True, max_call_depth=8,
        entry_keywords=[
            "peerscore", "scorepeer", "reputation", "peerdb", "scorer",
            "isbadpeer", "gossipsubscore", "subnetscorer", "peeraction",
            "recomputescore", "worstconnectedpeers", "pruneexcesspeers",
            "applyreconnectioncooldown", "banpeer", "disconnectpeer",
        ],
        entry_path_markers=[
            "p2p/peers/scorers", "peer_manager/peerdb", "peerdb/score",
            "gossipsub_scoring_parameters", "networking/eth2/gossip/subnets",
            "networking/p2p/reputation", "network/peers/score",
            "network/gossip/scoringparameters", "peers/score/store",
        ],
        retrieval_queries=[
            "peer score algorithm weighted sum component gossip bad status block",
            "peer score gossipsub threshold ban disconnect forced",
            "peer reputation penalty reward adjust large small disconnect ban hours",
            "peer bad responses max threshold isBadPeer gossipThreshold",
            "peer score recompute composite PeerAction halflife decay",
            "subnet scorer unique coverage committee 1000",
            "prune excess peers worst score sort disconnect",
            # app-layer / peerdb targeted (beacon_peer.md paths)
            "ScoreNoLock scorerWeight badResponses peerStatus gossipScorer blockProvider",
            "peerdb recompute_score ban forced_disconnect healthy trusted infinity",
            "ReputationAdjustment LARGE_PENALTY reward DISCONNECT_THRESHOLD ban",
            "applyReconnectionCooldown goodbye reason cooldown ban freeze duration",
            "MaxScore trusted peer score value INFINITY 100",
        ],
        aspect_hints=[
            {"id": "scoring_algorithm_shape",
             "desc": "Overall peer scoring algorithm structure: a weighted sum of components (list them with weights), a set of independent scores, or a composite (reputation + gossipsub*weight)."},
            {"id": "disconnect_threshold",
             "desc": "Score threshold at which a peer is force-disconnected."},
            {"id": "ban_threshold",
             "desc": "Score threshold at which a peer is banned."},
            {"id": "ban_cooldown_seconds",
             "desc": "How long (seconds) a banned peer stays banned/frozen before it can reconnect (e.g. 43200=12h, 1800=30min)."},
            {"id": "trusted_peer_value",
             "desc": "Score value assigned to trusted peers (e.g. INFINITY, MAX_SCORE=100)."},
            {"id": "score_decay_halflife",
             "desc": "Score decay half-life / interval (e.g. 10 minutes, per epoch, per slot)."},
            {"id": "gossipsub_bridge_weight",
             "desc": "How the libp2p gossipsub score is bridged into the app-layer peer score (the multiplier/weight, e.g. 0.0011875)."},
        ],
    ),
    Domain(
        id="peer_management",
        name="Peer Management",
        kind="subsystem",
        description="Connection limits, pruning, reconnection cooldown",
        extract_fsm=False, extract_parameters=True, max_call_depth=7,
        entry_keywords=[
            "peermanager", "peerconnection", "connectpeer", "disconnectpeer",
            "prune", "reconnect", "connectionlimit", "maxpeers", "targetpeers",
        ],
        entry_path_markers=[
            "peer_manager", "p2p/peers", "network/peers",
        ],
        retrieval_queries=[
            "peer connection limit target max peers prune excess",
            "peer reconnection cooldown goodbye reason backoff",
        ],
    ),
]

# WORKFLOW_IDS remains the canonical workflow list; this is the semantic alias.
WORKFLOW_DOMAIN_IDS: list[str] = list(WORKFLOW_IDS)

_DOMAIN_INDEX: dict[str, Domain] = {
    d.id: d for d in (_build_workflow_domains() + SUBSYSTEM_DOMAINS)
}


def get_domain(domain_id: str) -> Domain | None:
    return _DOMAIN_INDEX.get(domain_id)


def all_domains() -> list[Domain]:
    """All analysis domains: 7 workflows first, then subsystems."""
    return list(_DOMAIN_INDEX.values())


def workflow_domains() -> list[Domain]:
    return [d for d in _DOMAIN_INDEX.values() if d.kind == "workflow"]


def subsystem_domains() -> list[Domain]:
    return [d for d in _DOMAIN_INDEX.values() if d.kind == "subsystem"]


# ── Fault scenarios for Phase 2.5 scenario scan ─────────────────────────────
#
# Each Scenario defines a concrete abnormal situation that an Ethereum
# consensus node may encounter. The scan agent searches for how each client
# handles the scenario and maps findings back to the LSG.
#
# search_queries   — targeted RAG queries to locate the handling code
# relevant_workflows — which Phase 2 workflows to scan after convergence


@dataclass
class Scenario:
    id: str
    name: str
    trigger: str            # one-line trigger condition
    risk: str               # consequence of NOT handling this
    search_queries: list[str]
    relevant_workflows: list[str] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        id="sync_stall",
        name="Sync Stall",
        trigger="No new blocks confirmed for an extended period (N slots) during sync",
        risk="Node stalls on a stale fork and misses finality",
        search_queries=[
            "stall detection sync no progress timeout",
            "sync chain reset backoff retry peer rotation",
            "lastFetchedSlot checkProgress batchTimeout stalled",
            "peer rotation replacement on sync failure",
        ],
        relevant_workflows=["initial_sync", "regular_sync"],
    ),
    Scenario(
        id="reorg_during_sync",
        name="Reorg During Sync",
        trigger="A chain reorganization occurs during initial_sync, especially across an epoch boundary",
        risk="Inconsistent sync target or incorrect epoch-boundary state",
        search_queries=[
            "reorg during sync reset fork choice finalization",
            "finalized checkpoint changed mid-sync clear caches",
            "epoch boundary reorg justified finalized update",
            "handleReorg onReorg resetSyncChain clearRequestCaches",
        ],
        relevant_workflows=["initial_sync", "regular_sync"],
    ),
    Scenario(
        id="el_invalid_cascade",
        name="EL INVALID with Incorrect latestValidHash",
        trigger="engine_newPayload returns INVALID and latestValidHash points to the wrong ancestor",
        risk="Valid blocks are invalidated in a cascade and the node forks from the wrong ancestor",
        search_queries=[
            "latestValidHash INVALID cascade invalidate descendants rollback",
            "removeInvalidBlockAndState SetOptimisticToInvalid",
            "invalid payload latest valid hash verification ancestor",
            "fork choice rollback on invalid execution payload",
        ],
        relevant_workflows=["execute_layer_relation"],
    ),
    Scenario(
        id="el_syncing_stuck",
        name="EL Stuck in SYNCING (Optimistic Lock-in)",
        trigger="EL keeps returning SYNCING and the CL enters a permanent optimistic state",
        risk="Chain safety guaranteed by the EL is lost; an attacker can control the optimistic head",
        search_queries=[
            "optimistic depth limit exceeded threshold max",
            "optimistic sync stuck indefinitely timeout disconnected",
            "IsOptimisticBlock optimistic head depth limit",
            "EL syncing halt stop import optimistic chain exceeds",
        ],
        relevant_workflows=["execute_layer_relation", "regular_sync"],
    ),
    Scenario(
        id="gossip_flood_ddos",
        name="Gossip DDoS",
        trigger="A malicious peer floods gossip messages (blocks, attestations, blobs)",
        risk="CPU exhaustion, queue saturation, and starvation of legitimate messages",
        search_queries=[
            "rate limit gossip message processing queue bounded",
            "peer score penalty invalid gossip flood reject",
            "message queue max size bounded throttle",
            "gossip validation throttle rate limit peer ban",
        ],
        relevant_workflows=["regular_sync"],
    ),
    Scenario(
        id="missing_parent_flood",
        name="Orphan Block Flood",
        trigger="A large number of blocks with unknown parents arrive, possibly from an attacker",
        risk="Unbounded orphan queue causes memory exhaustion, or peers are penalized spuriously",
        search_queries=[
            "orphan block unknown parent queue bounded max",
            "missing parent request by root limit cache evict",
            "pending block cache max size orphan pool",
            "parent not found request peers limit flood",
        ],
        relevant_workflows=["regular_sync"],
    ),
    Scenario(
        id="slashing_sign_order",
        name="Signing vs. Slashing-Check Ordering",
        trigger="The ordering between a validator's signing operation and the slashing-DB check",
        risk="On crash recovery a validator double-votes/double-proposes and gets slashed",
        search_queries=[
            "slashing protection check before sign attestation block",
            "maySign checkAndInsert slashingDB BLS signature order",
            "sign then record slashing database crash recovery",
            "double sign protection signing root DB write",
        ],
        relevant_workflows=["attestation_generate", "block_generate"],
    ),
    Scenario(
        id="checkpoint_ws_violation",
        name="Checkpoint Weak-Subjectivity Violation",
        trigger="Checkpoint sync uses a stale checkpoint outside the weak-subjectivity window",
        risk="The node bootstraps from a malicious chain and cannot perceive true finality",
        search_queries=[
            "weak subjectivity period validation checkpoint epoch boundary",
            "WSCheckpoint isWithinWSPeriod validateAnchor too old",
            "checkpoint outside weak subjectivity window fail ban",
            "weak subjectivity check failure peer report error",
        ],
        relevant_workflows=["checkpoint_sync"],
    ),
]

# Scenario scan configuration
SCENARIO_SCAN_TOP_K: int = 6    # search results per query per scenario
SCENARIO_SCAN_MAX_SNIPPETS: int = 20  # max snippets fed to LLM per scenario

# Embedding models tried in order; the first available one wins
EMBEDDING_MODELS: list[str] = [
    "nomic-embed-code",
    "text-embedding-3-large",
    "all-MiniLM-L6-v2",
]

# LLM provider & model
LLM_PROVIDER: str = "anthropic"          # "anthropic" | "gemini" | "deepseek" | "glm"
LLM_MODEL: str = "claude-sonnet-4-6"
GEMINI_MODEL: str = "gemini-3.5-flash"
DEEPSEEK_MODEL: str = "deepseek-v4-pro"
GLM_MODEL: str = "glm-5.2"

# API base URLs (empty = provider default; CLI / env vars override)
ANTHROPIC_BASE_URL: str = ""
GEMINI_BASE_URL: str = ""
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
GLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"

