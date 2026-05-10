"""Offline preprocessing pipeline.

Implements 4 tasks:
  A. AST symbol extraction (tree-sitter)
  B. Call-graph construction
  C. Enhanced vector index (Chroma)
  D. BM25 exact-match index
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import (
    CLIENT_NAMES,
    CODE_BASE_PATH,
    ENTRY_POINT_KEYWORDS,
    ENTRY_POINT_OVERRIDES,
    LANGUAGE_GRAMMARS,
    PREPROCESS_PATH,
    WORKFLOW_IDS,
)

logger = logging.getLogger(__name__)

# Maximum recursion depth for AST traversal
MAX_AST_RECURSION_DEPTH: int = 100


# Data classes


@dataclass
class SymbolInfo:
    """Information about a single function / method extracted via AST."""

    file: str
    function_name: str
    qualified_name: str
    start_line: int
    end_line: int
    source_code: str = ""
    calls: list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)
    # ── Production / test provenance (audit-review remediation) ───────────
    # ``is_production`` = False ⇒ symbol is in a test / mock / fixture /
    # bench / example / generator path or inside ``#[cfg(test)] mod tests``
    # (Rust). ``test_reason`` carries the rule that fired.
    is_production: bool = True
    test_reason: str = ""


@dataclass
class CallGraph:
    """Directed call-graph with workflow entry-point annotations."""

    nodes: list[str] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    entry_points: dict[str, list[str]] = field(default_factory=dict)


# Tokenization helpers

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SNAKE_RE = re.compile(r"_+")


def tokenize_identifier(name: str) -> list[str]:
    """Split an identifier into sub-tokens.

    Examples
    --------
    >>> tokenize_identifier("runInitialSync")
    ['runInitialSync', 'run', 'Initial', 'Sync']
    >>> tokenize_identifier("process_chain_segment")
    ['process_chain_segment', 'process', 'chain', 'segment']
    """
    tokens: list[str] = [name]
    # camelCase
    parts = _CAMEL_RE.sub("_", name).split("_")
    parts = [p for p in parts if p]
    if len(parts) > 1:
        tokens.extend(parts)
    else:
        snake_parts = _SNAKE_RE.split(name)
        snake_parts = [p for p in snake_parts if p]
        if len(snake_parts) > 1:
            tokens.extend(snake_parts)
    return tokens


def tokenize_source(source: str) -> list[str]:
    """Tokenize source code into identifier-level tokens for BM25."""
    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)
    result: list[str] = []
    for tok in raw_tokens:
        result.extend(tokenize_identifier(tok))
    return result


# Task A — AST symbol extraction

# AST node-type mapping per language
_AST_CONFIG: dict[str, dict[str, Any]] = {
    "go": {
        "func_types": ["function_declaration", "method_declaration"],
        "call_types": ["call_expression"],
        "name_field": "name",
        "extensions": [".go"],
    },
    "rust": {
        "func_types": ["function_item"],
        "call_types": ["call_expression", "macro_invocation"],
        "name_field": "name",
        "extensions": [".rs"],
    },
    "java": {
        "func_types": ["method_declaration"],
        "call_types": ["method_invocation"],
        "name_field": "name",
        "extensions": [".java"],
    },
    "typescript": {
        "func_types": ["function_declaration", "method_definition"],
        "call_types": ["call_expression"],
        "name_field": "name",
        "extensions": [".ts", ".tsx", ".js", ".jsx"],
    },
}


def _get_parser(language: str):
    """Return a tree-sitter Parser configured for *language*."""
    try:
        import tree_sitter_go
        import tree_sitter_java
        import tree_sitter_rust
        import tree_sitter_typescript
        from tree_sitter import Language, Parser

        lang_map = {
            "go": Language(tree_sitter_go.language()),
            "rust": Language(tree_sitter_rust.language()),
            "java": Language(tree_sitter_java.language()),
            "typescript": Language(tree_sitter_typescript.language_typescript()),
        }
        parser = Parser(lang_map[language])
        return parser, lang_map[language]
    except ImportError:
        logger.warning("tree-sitter bindings not available; returning None")
        return None, None


def _find_name_node(node, name_field: str):
    """Extract the name of a function/method node via tree-sitter field API."""
    # Use child_by_field_name (looks up by grammar field, e.g. "name")
    # instead of matching child.type which would be "identifier" / "field_identifier".
    child = node.child_by_field_name(name_field)
    if child is not None:
        return child.text.decode("utf-8") if child.text else ""
    return ""


def _extract_calls(node, call_types: list[str], depth: int = 0) -> list[str]:
    """Walk the AST to collect callee names from call expressions."""
    calls: list[str] = []
    if depth > MAX_AST_RECURSION_DEPTH:
        return calls
    if node.type in call_types:
        # Try to get function name from first named child
        func_node = node.child_by_field_name("function") or (
            node.children[0] if node.children else None
        )
        if func_node is not None:
            name = func_node.text.decode("utf-8") if func_node.text else ""
            # Take last segment for qualified calls (e.g., "pkg.Func" → "Func")
            short = name.rsplit(".", 1)[-1] if name else ""
            if short:
                calls.append(short)
    for child in node.children:
        calls.extend(_extract_calls(child, call_types, depth + 1))
    return calls


def _walk_functions(node, func_types: list[str], name_field: str, call_types: list[str],
                    source_bytes: bytes, file_path: str,
                    *, language: str = "",
                    file_is_test: bool = False, file_test_reason: str = "",
                    cfg_test_ranges: list[tuple[int, int]] | None = None,
                    ) -> list[SymbolInfo]:
    """Recursively extract functions from the AST."""
    results: list[SymbolInfo] = []
    cfg_test_ranges = cfg_test_ranges or []

    if node.type in func_types:
        fn_name = _find_name_node(node, name_field) or "<anonymous>"
        start = node.start_point[0] + 1  # 1-based
        end = node.end_point[0] + 1
        body = source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        calls = _extract_calls(node, call_types)

        # Build qualified name: for Go methods, prepend receiver
        qualified = fn_name
        if node.type == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            if receiver is not None:
                receiver_text = receiver.text.decode("utf-8", errors="replace")
                qualified = f"({receiver_text}).{fn_name}"

        # ── Production vs test classification ────────────────────────────
        is_prod = not file_is_test
        test_reason = file_test_reason
        if is_prod:
            in_prod, fn_test_reason = _classify_function_provenance(
                fn_name=fn_name,
                body=body,
                start_line=start,
                end_line=end,
                language=language,
                cfg_test_ranges=cfg_test_ranges,
            )
            is_prod = in_prod
            if not in_prod:
                test_reason = fn_test_reason

        results.append(SymbolInfo(
            file=file_path,
            function_name=fn_name,
            qualified_name=qualified,
            start_line=start,
            end_line=end,
            source_code=body,
            calls=list(set(calls)),
            is_production=is_prod,
            test_reason=test_reason,
        ))
    for child in node.children:
        results.extend(_walk_functions(
            child, func_types, name_field, call_types, source_bytes, file_path,
            language=language,
            file_is_test=file_is_test,
            file_test_reason=file_test_reason,
            cfg_test_ranges=cfg_test_ranges,
        ))
    return results


# ── Production / test provenance helpers ─────────────────────────────────

# Function-name prefixes that signal test / bench / mock implementations.
_TEST_FN_PREFIXES = ("test", "fuzz", "bench", "mock", "fake", "stub", "dummy")


def _classify_function_provenance(
    *, fn_name: str, body: str, start_line: int, end_line: int,
    language: str, cfg_test_ranges: list[tuple[int, int]],
) -> tuple[bool, str]:
    """Return (is_production, reason) for a function whose file is production."""
    fn_lower = fn_name.lower().replace("_", "")

    # Rust: function falls inside a #[cfg(test)] mod block.
    if language == "rust":
        for lo, hi in cfg_test_ranges:
            if start_line >= lo and end_line <= hi:
                return False, f"inside #[cfg(test)] block L{lo}-L{hi}"
        head = body.lstrip().splitlines()[:4] if body else []
        for ln in head:
            s = ln.strip()
            if s.startswith("#[test]") or s.startswith("#[tokio::test"):
                return False, "Rust #[test] attribute"
            if s.startswith("#[cfg(test)]"):
                return False, "Rust #[cfg(test)] attribute"

    # Go: TestXxx / BenchmarkXxx / FuzzXxx / ExampleXxx.
    if language == "go":
        if any(fn_lower.startswith(p) for p in _TEST_FN_PREFIXES):
            return False, f"Go test/bench-style function name '{fn_name}'"

    # Java: JUnit @Test annotation immediately preceding the method body.
    if language == "java":
        head = body.lstrip().splitlines()[:3] if body else []
        for ln in head:
            s = ln.strip()
            if s.startswith("@Test") or s.startswith("@ParameterizedTest") \
                    or s.startswith("@RepeatedTest") or s.startswith("@TestFactory"):
                return False, "Java JUnit @Test annotation"

    return True, ""


# Path-level test-file detection (mirrors tools/source_reader rules but kept
# local to avoid importing source_reader during preprocessing).
_PATH_TEST_DIR_MARKERS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
    "/fixtures/", "/fixture/", "/mocks/", "/mock/", "/testing/",
    "/testdata/", "/test_data/", "/test-utils/", "/testutil/",
    "/test_helpers/", "/benches/", "/bench/", "/examples/",
)
_PATH_LANG_MARKERS: dict[str, tuple[str, ...]] = {
    "rust":       ("/src/bin/test_generator", "/test_generator/"),
    "go":         ("/testutil/", "/mock/", "/mocks/"),
    "java":       ("/src/test/", "/integration-test/"),
    "typescript": ("/__tests__/", "/test/"),
}
_FILENAME_TEST_SUFFIXES = (
    "_test.go", "_tests.rs",
    ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
    ".test.js", ".spec.js",
    "Test.java", "IT.java", "Tests.java",
)


def _classify_path(rel_path: str, language: str) -> tuple[bool, str]:
    """Return (is_test, reason) using path-only heuristics."""
    lower = rel_path.lower().replace("\\", "/")
    for m in _PATH_TEST_DIR_MARKERS:
        if m in lower:
            return True, f"path contains '{m.strip('/')}'"
    for m in _PATH_LANG_MARKERS.get(language, ()):
        if m in lower:
            return True, f"path contains '{m.strip('/')}' ({language})"
    for suf in _FILENAME_TEST_SUFFIXES:
        if rel_path.endswith(suf):
            return True, f"filename ends with '{suf}'"
    if "test_generator" in lower:
        return True, "path contains 'test_generator'"
    return False, ""


def _extract_symbols(client_name: str) -> list[SymbolInfo]:
    """Task A: Extract function symbols from client source code using tree-sitter."""
    lang_key, _grammar = LANGUAGE_GRAMMARS[client_name]
    ast_cfg = _AST_CONFIG[lang_key]
    code_dir = CODE_BASE_PATH / client_name

    if not code_dir.exists():
        logger.warning("Source directory %s does not exist — skipping AST extraction", code_dir)
        return []

    parser, _lang_obj = _get_parser(lang_key)
    if parser is None:
        logger.warning("tree-sitter parser unavailable for %s — returning empty symbols", lang_key)
        return []

    symbols: list[SymbolInfo] = []
    extensions = ast_cfg["extensions"]

    for root, _dirs, files in os.walk(code_dir):
        for fname in files:
            if not any(fname.endswith(ext) for ext in extensions):
                continue
            full_path = Path(root) / fname
            rel_path = str(full_path.relative_to(CODE_BASE_PATH / client_name))
            try:
                source = full_path.read_bytes()
                tree = parser.parse(source)
                file_is_test, file_test_reason = _classify_path(rel_path, lang_key)
                cfg_test_ranges: list[tuple[int, int]] = []
                if lang_key == "rust" and not file_is_test:
                    try:
                        cfg_test_ranges = _scan_rust_cfg_test_ranges(
                            source.decode("utf-8", errors="replace")
                        )
                    except Exception:  # pragma: no cover — defensive
                        cfg_test_ranges = []
                file_symbols = _walk_functions(
                    tree.root_node,
                    ast_cfg["func_types"],
                    ast_cfg["name_field"],
                    ast_cfg["call_types"],
                    source,
                    rel_path,
                    language=lang_key,
                    file_is_test=file_is_test,
                    file_test_reason=file_test_reason,
                    cfg_test_ranges=cfg_test_ranges,
                )
                symbols.extend(file_symbols)
            except Exception:
                logger.debug("Failed to parse %s", full_path, exc_info=True)

    n_prod = sum(1 for s in symbols if s.is_production)
    logger.info(
        "[_extract_symbols] client=%s symbols=%d production=%d test=%d",
        client_name, len(symbols), n_prod, len(symbols) - n_prod,
    )
    return symbols


# ── Rust cfg(test) range scanner (pure regex, no tree-sitter) ────────────

_RE_CFG_TEST = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]")


def _scan_rust_cfg_test_ranges(text: str) -> list[tuple[int, int]]:
    """Return inclusive 1-based line ranges of ``#[cfg(test)] mod tests {...}``."""
    lines = text.splitlines()
    ranges: list[tuple[int, int]] = []
    pending = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if _RE_CFG_TEST.search(s):
            pending = True
            i += 1
            continue
        if pending and ("mod " in s or s.startswith("fn ") or "fn " in s):
            depth = 0
            opened = False
            start = i + 1
            j = i
            done = False
            while j < len(lines) and not done:
                for ch in lines[j]:
                    if ch == "{":
                        depth += 1
                        opened = True
                    elif ch == "}":
                        depth -= 1
                        if opened and depth == 0:
                            ranges.append((start, j + 1))
                            done = True
                            break
                j += 1
            pending = False
            i = max(j, i + 1)
            continue
        if pending and s and not s.startswith(("//", "#[")):
            pending = False
        i += 1
    return ranges


# Task B — Call-graph construction


def _build_callgraph(client_name: str, symbols: list[SymbolInfo]) -> CallGraph:
    """Task B: Build a directed call-graph and identify workflow entry points."""
    name_to_symbol: dict[str, SymbolInfo] = {}
    for sym in symbols:
        name_to_symbol[sym.function_name] = sym
        name_to_symbol[sym.qualified_name] = sym

    nodes_set: set[str] = set()
    edges: list[dict[str, str]] = []

    for sym in symbols:
        nodes_set.add(sym.qualified_name)
        for callee_short in sym.calls:
            if callee_short in name_to_symbol:
                callee_qn = name_to_symbol[callee_short].qualified_name
                edges.append({"caller": sym.qualified_name, "callee": callee_qn})
                nodes_set.add(callee_qn)
                # Back-link
                name_to_symbol[callee_short].called_by.append(sym.qualified_name)

    # ── Entry-point detection ───────────────────────────────────────────
    overrides = ENTRY_POINT_OVERRIDES.get(client_name, {})
    entry_points: dict[str, list[str]] = {}

    for wf_id in WORKFLOW_IDS:
        if wf_id in overrides:
            entry_points[wf_id] = overrides[wf_id]
            continue

        keywords = ENTRY_POINT_KEYWORDS.get(wf_id, [])
        matched: list[str] = []
        for sym in symbols:
            # Skip non-production symbols entirely — entry points must be real.
            if not sym.is_production:
                continue
            fn_lower = sym.function_name.lower().replace("_", "")
            if any(kw in fn_lower for kw in keywords):
                matched.append(sym.qualified_name)
        entry_points[wf_id] = matched

    cg = CallGraph(
        nodes=sorted(nodes_set),
        edges=edges,
        entry_points=entry_points,
    )
    logger.info(
        "[_build_callgraph] client=%s nodes=%d edges=%d",
        client_name,
        len(cg.nodes),
        len(cg.edges),
    )
    return cg


# Task C — Enhanced vector index


def _compute_call_depths(callgraph: CallGraph) -> dict[str, tuple[int, list[str]]]:
    """BFS from entry points to compute min call depth & workflow hints for each node."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in callgraph.edges:
        adjacency[edge["caller"]].append(edge["callee"])

    depths: dict[str, int] = {}
    hints: dict[str, set[str]] = defaultdict(set)

    for wf_id, entries in callgraph.entry_points.items():
        queue: deque[tuple[str, int]] = deque()
        visited: set[str] = set()
        for ep in entries:
            queue.append((ep, 0))
            visited.add(ep)

        while queue:
            node, depth = queue.popleft()
            if node not in depths or depth < depths[node]:
                depths[node] = depth
            hints[node].add(wf_id)

            for callee in adjacency.get(node, []):
                if callee not in visited:
                    visited.add(callee)
                    queue.append((callee, depth + 1))

    result: dict[str, tuple[int, list[str]]] = {}
    for node in set(list(depths.keys()) + list(hints.keys())):
        result[node] = (depths.get(node, 999), sorted(hints.get(node, set())))
    return result


def _build_vector_index(client_name: str, symbols: list[SymbolInfo], callgraph: CallGraph) -> None:
    """Task C: Build Chroma vector index with call-graph enhanced metadata."""
    try:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        from langchain_chroma import Chroma
        from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
    except ImportError:
        logger.warning("langchain/chroma dependencies not available — skipping vector index build")
        return

    lang_key, _ = LANGUAGE_GRAMMARS[client_name]
    lang_map = {
        "go": Language.GO,
        "rust": Language.RUST,
        "java": Language.JAVA,
        "typescript": Language.TS,
    }
    ts_lang = lang_map.get(lang_key, Language.GO)

    splitter = RecursiveCharacterTextSplitter.from_language(
        language=ts_lang,
        chunk_size=2000,
        chunk_overlap=200,
    )

    depths_map = _compute_call_depths(callgraph)
    name_to_sym: dict[str, SymbolInfo] = {}
    for sym in symbols:
        name_to_sym[sym.qualified_name] = sym

    # Adjacency for caller lookup
    callee_to_callers: dict[str, list[str]] = defaultdict(list)
    caller_to_callees: dict[str, list[str]] = defaultdict(list)
    for edge in callgraph.edges:
        callee_to_callers[edge["callee"]].append(edge["caller"])
        caller_to_callees[edge["caller"]].append(edge["callee"])

    documents = []
    metadatas = []
    ids = []

    for idx, sym in enumerate(symbols):
        if not sym.source_code.strip():
            continue

        chunks = splitter.split_text(sym.source_code)
        depth, wf_hints = depths_map.get(sym.qualified_name, (999, []))

        for chunk_idx, chunk in enumerate(chunks):
            doc_id = f"{client_name}_{idx}_{chunk_idx}"
            meta = {
                "client_name": client_name,
                "language": lang_key,
                "file_path": sym.file,
                "function_name": sym.function_name,
                "qualified_name": sym.qualified_name,
                "start_line": sym.start_line,
                "end_line": sym.end_line,
                "call_depth": depth,
                "workflow_hints": ",".join(wf_hints),
                "callers": ",".join(callee_to_callers.get(sym.qualified_name, [])[:10]),
                "callees": ",".join(caller_to_callees.get(sym.qualified_name, [])[:10]),
                # ── Production / test provenance (audit-review remediation) ──
                "is_production": bool(sym.is_production),
                "test_reason":   sym.test_reason or "",
            }
            documents.append(chunk)
            metadatas.append(meta)
            ids.append(doc_id)

    if not documents:
        logger.info("[_build_vector_index] No documents to index for %s", client_name)
        return

    persist_dir = str(PREPROCESS_PATH / f"{client_name}_chroma")
    try:
        embedding = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        db = Chroma.from_texts(
            texts=documents,
            metadatas=metadatas,
            ids=ids,
            embedding=embedding,
            collection_name=client_name,
            persist_directory=persist_dir,
        )
        logger.info(
            "[_build_vector_index] client=%s docs=%d persist=%s",
            client_name,
            len(documents),
            persist_dir,
        )
    except Exception:
        logger.error("Failed to build vector index for %s", client_name, exc_info=True)


# Task D — BM25 exact-match index


def _build_bm25_index(client_name: str, symbols: list[SymbolInfo]) -> None:
    """Task D: Build BM25 index from tokenized function bodies."""
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("rank_bm25 not installed — skipping BM25 index build")
        return

    corpus: list[list[str]] = []
    doc_metadata: list[dict[str, Any]] = []

    for sym in symbols:
        if not sym.source_code.strip():
            continue
        tokens = tokenize_source(sym.source_code)
        corpus.append(tokens)
        doc_metadata.append({
            "client_name": client_name,
            "file_path": sym.file,
            "function_name": sym.function_name,
            "qualified_name": sym.qualified_name,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            "source_code": sym.source_code,
            "is_production": bool(sym.is_production),
            "test_reason":   sym.test_reason or "",
        })

    if not corpus:
        logger.info("[_build_bm25_index] No corpus for %s", client_name)
        return

    bm25 = BM25Okapi(corpus)
    out_path = PREPROCESS_PATH / f"{client_name}_bm25.pkl"
    with open(out_path, "wb") as f:
        pickle.dump({"bm25": bm25, "corpus": corpus, "metadata": doc_metadata}, f)

    logger.info("[_build_bm25_index] client=%s docs=%d path=%s", client_name, len(corpus), out_path)


# Public entry point


def _artifacts_exist(client_name: str) -> bool:
    """Check if all preprocessing artifacts already exist for *client_name*."""
    base = PREPROCESS_PATH
    return all([
        (base / f"{client_name}_symbols.json").exists(),
        (base / f"{client_name}_callgraph.json").exists(),
        (base / f"{client_name}_bm25.pkl").exists(),
        (base / f"{client_name}_chroma").exists(),
    ])


def run_preprocessing(client_name: str, force_rebuild: bool = False) -> dict[str, bool]:
    """Run the full preprocessing pipeline for a single client.

    Returns a dict compatible with PreprocessStatus fields.
    """
    PREPROCESS_PATH.mkdir(parents=True, exist_ok=True)

    if not force_rebuild and _artifacts_exist(client_name):
        logger.info("[run_preprocessing] client=%s — all artifacts exist, skipping", client_name)
        return {
            "symbols_ready": True,
            "callgraph_ready": True,
            "vector_index_ready": True,
            "bm25_index_ready": True,
        }

    status = {
        "symbols_ready": False,
        "callgraph_ready": False,
        "vector_index_ready": False,
        "bm25_index_ready": False,
    }

    # Task A
    symbols = _extract_symbols(client_name)
    sym_path = PREPROCESS_PATH / f"{client_name}_symbols.json"
    with open(sym_path, "w") as f:
        json.dump([asdict(s) for s in symbols], f, indent=2)
    status["symbols_ready"] = True

    # Task B
    callgraph = _build_callgraph(client_name, symbols)
    cg_path = PREPROCESS_PATH / f"{client_name}_callgraph.json"
    with open(cg_path, "w") as f:
        json.dump(asdict(callgraph), f, indent=2)
    status["callgraph_ready"] = True

    # Task C
    _build_vector_index(client_name, symbols, callgraph)
    status["vector_index_ready"] = True

    # Task D
    _build_bm25_index(client_name, symbols)
    status["bm25_index_ready"] = True

    return status


def run_all_preprocessing(force_rebuild: bool = False) -> dict[str, dict[str, bool]]:
    """Run preprocessing for all configured clients."""
    results: dict[str, dict[str, bool]] = {}
    for client in CLIENT_NAMES:
        results[client] = run_preprocessing(client, force_rebuild=force_rebuild)
    return results
