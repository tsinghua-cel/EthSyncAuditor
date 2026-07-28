"""Hybrid search tools for RAG retrieval.

Provides two search modes:
  Mode A: search_codebase  — semantic hybrid search (Phase 1)
  Mode B: search_codebase_by_workflow — call-graph directed hybrid (Phase 2)
"""

from __future__ import annotations

import json
import logging
import pickle
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    BM25_WEIGHT,
    PREPROCESS_PATH,
    VECTOR_WEIGHT,
)
from tools.preprocessor import tokenize_source

logger = logging.getLogger(__name__)


# Lightweight document wrapper (avoids hard dep on langchain Document)


@dataclass
class SearchResult:
    """Minimal document returned by search tools."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


# Index loaders (lazy, cached per client)

_bm25_cache: dict[str, dict] = {}
_callgraph_cache: dict[str, dict] = {}
_chroma_cache: dict[str, Any] = {}
_embeddings_singleton: Any = None


def _get_embeddings():
    """Return a process-wide cached HuggingFaceEmbeddings instance.

    Loading the embedding model is expensive (~seconds + GPU/mps memory).
    Previously _load_chroma re-instantiated it on every search call, leaking
    memory until the run crashed; cache it once.
    """
    global _embeddings_singleton
    if _embeddings_singleton is not None:
        return _embeddings_singleton
    try:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings
    except ImportError:
        logger.warning("HuggingFaceEmbeddings not available")
        return None
    _embeddings_singleton = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return _embeddings_singleton
_callgraph_cache: dict[str, dict] = {}


def _load_bm25(client_name: str) -> dict | None:
    if client_name in _bm25_cache:
        return _bm25_cache[client_name]
    path = PREPROCESS_PATH / f"{client_name}_bm25.pkl"
    if not path.exists():
        logger.warning("BM25 index not found: %s", path)
        return None
    # Safe to use pickle.load here: the BM25 index is only written by our own
    # preprocessing pipeline (_build_bm25_index) and never from external sources.
    with open(path, "rb") as f:
        data = pickle.load(f)  # noqa: S301
    _bm25_cache[client_name] = data
    return data


def _load_callgraph(client_name: str) -> dict | None:
    if client_name in _callgraph_cache:
        return _callgraph_cache[client_name]
    path = PREPROCESS_PATH / f"{client_name}_callgraph.json"
    if not path.exists():
        logger.warning("Call-graph not found: %s", path)
        return None
    with open(path) as f:
        data = json.load(f)
    _callgraph_cache[client_name] = data
    return data


def _load_chroma(client_name: str):
    """Load (and cache) a Chroma collection for *client_name*."""
    if client_name in _chroma_cache:
        return _chroma_cache[client_name]
    try:
        from langchain_chroma import Chroma
    except ImportError:
        logger.warning("Chroma/langchain deps not available")
        return None
    persist_dir = str(PREPROCESS_PATH / f"{client_name}_chroma")
    if not Path(persist_dir).exists():
        logger.warning("Chroma dir not found: %s", persist_dir)
        return None
    embedding = _get_embeddings()
    if embedding is None:
        return None
    db = Chroma(
        collection_name=client_name,
        persist_directory=persist_dir,
        embedding_function=embedding,
    )
    _chroma_cache[client_name] = db
    return db


# BM25 search helper


def _bm25_search(
    query: str,
    client_name: str,
    top_k: int = 5,
    allowed_functions: set[str] | None = None,
) -> list[SearchResult]:
    """Search the BM25 index and return top_k results."""
    bm25_data = _load_bm25(client_name)
    if bm25_data is None:
        return []

    bm25 = bm25_data["bm25"]
    metadata_list: list[dict] = bm25_data["metadata"]

    query_tokens = tokenize_source(query)
    scores = bm25.get_scores(query_tokens)

    indexed: list[tuple[int, float]] = list(enumerate(scores))

    if allowed_functions is not None:
        indexed = [
            (i, s) for i, s in indexed
            if metadata_list[i].get("qualified_name") in allowed_functions
            or metadata_list[i].get("function_name") in allowed_functions
        ]

    indexed.sort(key=lambda x: x[1], reverse=True)
    results: list[SearchResult] = []
    for i, score in indexed[:top_k]:
        meta = metadata_list[i]
        results.append(SearchResult(
            content=meta.get("source_code", ""),
            metadata=meta,
            score=float(score),
        ))
    return results


# Vector search helper


def _vector_search(
    query: str,
    client_name: str,
    top_k: int = 5,
    filter_dict: dict | None = None,
    allowed_functions: set[str] | None = None,
) -> list[SearchResult]:
    """Search Chroma vector store and return top_k results.

    F2: when *allowed_functions* is set (the call-graph reachable set), over-
    retrieve (k = max(top_k*3, 20)) then filter to that set. Previously the
    vector leg ran unfiltered against the whole index and its hits were almost
    never in the workflow subgraph, so they were post-hoc discarded and hybrid
    search degraded to BM25-only.
    """
    db = _load_chroma(client_name)
    if db is None:
        return []

    fetch_k = max(top_k * 3, 20) if allowed_functions else top_k
    try:
        results = db.similarity_search_with_relevance_scores(
            query,
            k=fetch_k,
            filter=filter_dict,
        )
    except Exception:
        logger.debug("Vector search failed for %s", client_name, exc_info=True)
        return []

    out: list[SearchResult] = []
    for doc, score in results:
        meta = doc.metadata
        if allowed_functions is not None:
            qn = meta.get("qualified_name", "")
            fn = meta.get("function_name", "")
            if qn not in allowed_functions and fn not in allowed_functions:
                continue
        out.append(SearchResult(content=doc.page_content, metadata=meta, score=float(score)))
        if len(out) >= top_k:
            break
    return out


# Mode A — semantic hybrid search (Phase 1)


def search_codebase(
    query: str,
    client_name: str,
    top_k: int = 5,
) -> list[SearchResult]:
    """Execute hybrid search (BM25 + vector) with weighted fusion.

    Parameters
    ----------
    query : str
        Natural-language or keyword query.
    client_name : str
        Which client's index to search.
    top_k : int
        Number of results to return after fusion.

    Returns
    -------
    list[SearchResult]
    """
    bm25_results = _bm25_search(query, client_name, top_k=top_k)
    vector_results = _vector_search(query, client_name, top_k=top_k)

    return _fuse_results(bm25_results, vector_results, top_k)


# Mode B — call-graph directed hybrid search (Phase 2 / parameter track)


def search_codebase_by_domain(
    domain_id: str,
    query: str,
    client_name: str,
    max_call_depth: int = 7,
    top_k: int = 10,
) -> list[SearchResult]:
    """Call-graph directed hybrid search for one analysis domain.

    *domain_id* may be a workflow id or a subsystem domain id (discv5,
    peer_scoring, ...). 1. Look up entry_points[domain_id] in the callgraph.
    2. BFS up to *max_call_depth*. 3. Hybrid search within the reachable
    function set (F2: the vector leg over-retrieves then filters to the set).
    4. Sort by call_depth ascending. Falls back to whole-index search when no
    callgraph or no entry points exist for the domain.
    """
    cg = _load_callgraph(client_name)
    if cg is None:
        logger.warning("No callgraph for %s — falling back to full search", client_name)
        return search_codebase(query, client_name, top_k)

    entry_fns: list[str] = cg.get("entry_points", {}).get(domain_id, [])
    if not entry_fns:
        logger.info("No entry points for %s/%s — full search fallback", client_name, domain_id)
        return search_codebase(query, client_name, top_k)

    # Build adjacency
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in cg.get("edges", []):
        adjacency[edge["caller"]].append(edge["callee"])

    # BFS
    reachable: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for ep in entry_fns:
        queue.append((ep, 0))
        reachable.add(ep)

    while queue:
        node, depth = queue.popleft()
        if depth >= max_call_depth:
            continue
        for callee in adjacency.get(node, []):
            if callee not in reachable:
                reachable.add(callee)
                queue.append((callee, depth + 1))

    # Search within reachable set (F2: vector leg filters to the set).
    bm25_results = _bm25_search(query, client_name, top_k=top_k, allowed_functions=reachable)
    vector_results = _vector_search(query, client_name, top_k=top_k, allowed_functions=reachable)

    fused = _fuse_results(bm25_results, vector_results, top_k)

    # Sort by call_depth ascending
    fused.sort(key=lambda r: r.metadata.get("call_depth", 999))
    return fused


def search_codebase_by_workflow(
    workflow_id: str,
    query: str,
    client_name: str,
    max_call_depth: int = 7,   # F3: was 5 — error/recovery subgraphs live deeper
    top_k: int = 10,
) -> list[SearchResult]:
    """Call-graph directed hybrid search for a workflow domain.

    Thin wrapper over :func:`search_codebase_by_domain` (a workflow id is a
    domain id). Kept for backward compatibility with existing callers/tests.
    """
    return search_codebase_by_domain(
        workflow_id, query, client_name, max_call_depth=max_call_depth, top_k=top_k,
    )


# Fusion helper


def _fuse_results(
    bm25_results: list[SearchResult],
    vector_results: list[SearchResult],
    top_k: int,
) -> list[SearchResult]:
    """Weighted reciprocal-rank fusion of BM25 and vector results."""
    score_map: dict[str, float] = {}
    doc_map: dict[str, SearchResult] = {}

    def _key(r: SearchResult) -> str:
        return f"{r.metadata.get('qualified_name', '')}:{r.metadata.get('start_line', 0)}"

    for rank, r in enumerate(bm25_results):
        k = _key(r)
        rr_score = 1.0 / (rank + 1)
        score_map[k] = score_map.get(k, 0.0) + BM25_WEIGHT * rr_score
        doc_map[k] = r

    for rank, r in enumerate(vector_results):
        k = _key(r)
        rr_score = 1.0 / (rank + 1)
        score_map[k] = score_map.get(k, 0.0) + VECTOR_WEIGHT * rr_score
        if k not in doc_map:
            doc_map[k] = r

    sorted_keys = sorted(score_map, key=lambda k: score_map[k], reverse=True)
    results: list[SearchResult] = []
    for k in sorted_keys[:top_k]:
        doc = doc_map[k]
        doc.score = score_map[k]
        results.append(doc)
    return results
