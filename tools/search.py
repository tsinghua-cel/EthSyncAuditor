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
    """Load a Chroma collection for *client_name*."""
    try:
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        from langchain_chroma import Chroma
    except ImportError:
        logger.warning("Chroma/langchain deps not available")
        return None
    persist_dir = str(PREPROCESS_PATH / f"{client_name}_chroma")
    if not Path(persist_dir).exists():
        logger.warning("Chroma dir not found: %s", persist_dir)
        return None
    embedding = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return Chroma(
        collection_name=client_name,
        persist_directory=persist_dir,
        embedding_function=embedding,
    )


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
) -> list[SearchResult]:
    """Search Chroma vector store and return top_k results."""
    db = _load_chroma(client_name)
    if db is None:
        return []

    try:
        results = db.similarity_search_with_relevance_scores(
            query,
            k=top_k,
            filter=filter_dict,
        )
    except Exception:
        logger.debug("Vector search failed for %s", client_name, exc_info=True)
        return []

    out: list[SearchResult] = []
    for doc, score in results:
        out.append(SearchResult(
            content=doc.page_content,
            metadata=doc.metadata,
            score=float(score),
        ))
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


# Mode B — call-graph directed hybrid search (Phase 2)


def search_codebase_by_workflow(
    workflow_id: str,
    query: str,
    client_name: str,
    max_call_depth: int = 5,
    top_k: int = 10,
) -> list[SearchResult]:
    """Call-graph directed hybrid search for LSG extraction.

    1. From callgraph, get entry_points for *workflow_id*.
    2. BFS up to *max_call_depth*, collecting reachable function names.
    3. Search within that function set using hybrid retrieval.
    4. Sort results by call_depth ascending (closest to entry first).
    """
    cg = _load_callgraph(client_name)
    if cg is None:
        logger.warning("No callgraph for %s — falling back to full search", client_name)
        return search_codebase(query, client_name, top_k)

    entry_fns: list[str] = cg.get("entry_points", {}).get(workflow_id, [])
    if not entry_fns:
        logger.info("No entry points for %s/%s — full search fallback", client_name, workflow_id)
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

    # Search within reachable set
    bm25_results = _bm25_search(query, client_name, top_k=top_k, allowed_functions=reachable)
    vector_results = _vector_search(query, client_name, top_k=top_k)

    # Filter vector results to reachable set
    filtered_vector: list[SearchResult] = []
    for r in vector_results:
        qn = r.metadata.get("qualified_name", "")
        fn = r.metadata.get("function_name", "")
        if qn in reachable or fn in reachable:
            filtered_vector.append(r)

    fused = _fuse_results(bm25_results, filtered_vector, top_k)

    # Sort by call_depth ascending
    fused.sort(key=lambda r: r.metadata.get("call_depth", 999))
    return fused


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
