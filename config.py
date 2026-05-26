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


# LLM provider & model
LLM_PROVIDER: str = "anthropic"          # "anthropic" | "gemini"
LLM_MODEL: str = "claude-opus-4-6"
GEMINI_MODEL: str = "gemini-2.5-flash"


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
ENTRY_POINT_KEYWORDS: dict[str, list[str]] = {
    "initial_sync": [
        "initialsync", "runinitial", "startinitial",
        "rangesync", "syncingchain", "syncchain",
        "forwardsync", "peersync", "syncmanager",
        "syncblockbyrange",
    ],
    "regular_sync": [
        "regularsync", "runregular", "gossipsync",
        "receiveblock", "receiveattestation", "processblock",
        "gossiphandler", "blockimporter", "gossipvalidator",
        "handlereorg", "onreorg",
    ],
    "checkpoint_sync": [
        "checkpointsync", "runcheckpoint",
        "backfillsync", "backfillbatch",
        "weaksubjectivity",
    ],
    "block_generate": [
        "proposeblock", "buildblock", "produceblock",
        "builderapi", "builderbid", "mevboost",
        "enginegetpayload", "buildergetpayload",
        "blockproductionduty", "blockservice",
    ],
    "attestation_generate": [
        "submitattestation", "createattestation",
        "attestationservice", "attestationduty", "attestationproduction",
        "performattestationduty",
        "slashingprotection", "issafetoattest",
    ],
    "aggregate": [
        "aggregateandproof", "submitaggregate",
        "aggregationduty", "aggregatorselection",
        "produceaggregate", "publishaggregate",
        "computeaggregate",
    ],
    "execute_layer_relation": [
        "engineapi", "executionengine", "forkchoiceupdate",
        "newpayload", "notifynewpayload", "payloadstatus",
        "optimisticsync", "optimisticimport",
        "invalidpayload", "invalidateblock",
    ],
}

# Per-client entry-point overrides (client → workflow → list[fn])
ENTRY_POINT_OVERRIDES: dict[str, dict[str, list[str]]] = {}

# Embedding models tried in order; the first available one wins
EMBEDDING_MODELS: list[str] = [
    "nomic-embed-code",
    "text-embedding-3-large",
    "all-MiniLM-L6-v2",
]

# API base URLs (empty = provider default; CLI / env vars override)
ANTHROPIC_BASE_URL: str = ""
GEMINI_BASE_URL: str = ""

