"""Centralised configuration constants."""

from __future__ import annotations

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
MAX_ITER_B_CLASS: int = 3
B_CLASS_STABLE_WINDOW: int = 2
B_CLASS_CHANGE_THRESHOLD: int = 1

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

# Embedding models tried in order; the first available one wins
EMBEDDING_MODELS: list[str] = [
    "nomic-embed-code",
    "text-embedding-3-large",
    "all-MiniLM-L6-v2",
]

# LLM provider & model
LLM_PROVIDER: str = "anthropic"          # "anthropic" | "gemini" | "deepseek"
LLM_MODEL: str = "claude-opus-4-6"
GEMINI_MODEL: str = "gemini-3.5-flash"
DEEPSEEK_MODEL: str = "deepseek-v4-pro"

# API base URLs (empty = provider default; CLI / env vars override)
ANTHROPIC_BASE_URL: str = ""
GEMINI_BASE_URL: str = ""
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

