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
        name="同步停滞",
        trigger="同步进程中长时间（N slot）无新区块确认",
        risk="节点卡在过期分叉，错过最终确定性",
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
        name="同步中 Reorg",
        trigger="initial_sync 过程中发生链重组，尤其跨 epoch 边界",
        risk="sync target 不一致、epoch 边界状态错误",
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
        name="EL INVALID + 错误 latestValidHash",
        trigger="engine_newPayload 返回 INVALID，latestValidHash 指向错误祖先",
        risk="正确区块被级联作废，节点从错误祖先开始分叉",
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
        name="EL 持续 SYNCING（optimistic 锁定）",
        trigger="EL 长期返回 SYNCING，CL 进入永久 optimistic 状态",
        risk="链安全由 EL 保障失效，攻击者可控制 optimistic head",
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
        trigger="恶意 peer 大量发送 gossip 消息（区块、attestation、blob）",
        risk="CPU 耗尽、队列满、正常消息被挤出",
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
        name="大量孤儿区块涌入",
        trigger="收到大量 parent 未知的区块，可能来自攻击者",
        risk="orphan 队列无界，内存耗尽；或 peer 被无效惩罚",
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
        name="签名与 slashing 检查顺序",
        trigger="验证者签名操作与 slashing DB 检查的时序关系",
        risk="崩溃恢复时产生双重投票/提案，验证者被 slash",
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
        name="Checkpoint 弱主观性违规",
        trigger="checkpoint sync 使用过期（超出 WS 窗口）的 checkpoint",
        risk="节点从恶意链 bootstrap，无法感知真实最终确定性",
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
LLM_PROVIDER: str = "anthropic"          # "anthropic" | "gemini" | "deepseek"
LLM_MODEL: str = "claude-opus-4-6"
GEMINI_MODEL: str = "gemini-3.5-flash"
DEEPSEEK_MODEL: str = "deepseek-v4-pro"

# API base URLs (empty = provider default; CLI / env vars override)
ANTHROPIC_BASE_URL: str = ""
GEMINI_BASE_URL: str = ""
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

