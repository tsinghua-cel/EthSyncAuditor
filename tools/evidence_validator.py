"""Hard-rule evidence validator.

Given an ``evidence`` block produced by an LLM agent, decide — without
further LLM calls — whether the cited code:

1. actually exists at the cited line range,
2. lives in production code (not ``tests/`` / ``mod tests`` / mock /
   ``test_generator`` / fixture / bench / example),
3. supports a finding's severity.

The validator returns a structured :class:`EvidenceVerdict` and a
recommended action: :data:`KEEP`, :data:`DOWNGRADE`, or :data:`DROP`.

This module is **the gate** that the audit-review report flagged as
missing — the previous pipeline let LLM judgements about test/production
provenance pass through unchecked, which is how 22 of 25 ``VULN-*``
findings ended up citing ``#[cfg(test)] mod tests`` blocks, ``bin/
test_generator.rs``, mocks or non-existent files.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from config import LANGUAGE_GRAMMARS
from tools.source_reader import (
    detect_test_block,
    is_test_path,
    read_source_lines,
)

logger = logging.getLogger(__name__)


# Verdict actions
KEEP      = "KEEP"
DOWNGRADE = "DOWNGRADE"
DROP      = "DROP"


# Patterns that appear inside production code but are too weak to count
# as a remote-exploitable vulnerability on their own. Findings whose
# evidence boils down to one of these get downgraded to ``MINOR / lead``.
_WEAK_INVARIANT_PATTERNS = (
    re.compile(r"\bdebug_assert!\s*\("),
    re.compile(r"\bassert!\s*\("),
    re.compile(r"\bassert_eq!\s*\("),
    re.compile(r"\bunreachable!\s*\("),
    re.compile(r"\.expect\s*\(\s*['\"]"),
    re.compile(r"\bpanic!\s*\("),
    re.compile(r"\bunimplemented!\s*\("),
)


@dataclass
class EvidenceVerdict:
    """Per-evidence verdict.

    Attributes
    ----------
    action
        One of :data:`KEEP`, :data:`DOWNGRADE`, :data:`DROP`.
    is_production
        ``True`` iff the cited file/lines are in production code.
    file_exists
        ``True`` iff the cited file resolves under ``code/<client>/``.
    line_verified
        ``True`` iff the cited line range fits within the resolved file.
    in_test_block
        ``True`` iff the cited slice is inside a ``#[cfg(test)] mod
        tests``, ``describe()/it()``, ``func TestXxx``, ``@Test`` etc.
    weak_invariant_only
        ``True`` iff the cited slice contains only ``debug_assert!`` /
        ``expect("...")`` / ``panic!`` style invariants — not a remote
        exploit primitive.
    test_reason / downgrade_reason
        Human-readable explanation, written into the report.
    excerpt
        Up to ~200 lines of the actual cited source — replaces whatever
        text the LLM hallucinated.
    """

    action: str = KEEP
    is_production: bool = True
    file_exists: bool = False
    line_verified: bool = False
    in_test_block: bool = False
    weak_invariant_only: bool = False
    test_reason: str = ""
    downgrade_reason: str = ""
    resolved_path: str = ""
    total_lines: int = 0
    excerpt: str = ""
    # ── P1 hardening: claim-grounding (audit-review remediation) ─────
    # ``True`` iff the cited snippet contains at least one significant
    # identifier from the finding's guard / description / security_note.
    # When the LLM stitches an unrelated source location into a finding
    # (e.g. citing ``engineNewPayload`` for a claim about
    # ``EnsureBalanceWithdrawalSourceAccountLimits``), this flag is False.
    claim_grounded: bool = True
    claim_hits: int = 0
    claim_keywords_checked: int = 0
    # Identifiers extracted from the snippet, used by the finding-level
    # anti-stitching check.
    snippet_identifiers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FindingVerdict:
    """Aggregate verdict for a finding (which may have evidence per client)."""

    action: str = KEEP                       # KEEP | DOWNGRADE | DROP
    evidence_quality: str = "STRONG"         # STRONG | WEAK | INVALID
    is_production: bool = True
    line_verified: bool = True
    downgrade_reasons: list[str] = field(default_factory=list)
    per_client: dict[str, EvidenceVerdict] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "evidence_quality": self.evidence_quality,
            "is_production": self.is_production,
            "line_verified": self.line_verified,
            "downgrade_reasons": list(self.downgrade_reasons),
            "per_client": {k: v.to_dict() for k, v in self.per_client.items()},
        }


# ---------------------------------------------------------------------------
# Single-evidence validation
# ---------------------------------------------------------------------------

# ── P1 hardening: claim-grounding (audit-review remediation) ────────────────

# Identifiers that are too generic to count as evidence-grounding hits.
# These dilute every snippet, so excluding them is essential.
_CLAIM_STOPWORDS = frozenset({
    # English filler
    "the", "and", "for", "with", "from", "into", "this", "that", "these",
    "those", "when", "where", "what", "while", "which", "have", "has",
    "does", "should", "must", "shall", "will", "would", "could", "may",
    "than", "then", "after", "before", "upon", "their", "them", "they",
    # generic concepts
    "true", "false", "none", "null", "value", "values", "result", "block",
    "blocks", "slot", "slots", "epoch", "epochs", "state", "states",
    "node", "nodes", "client", "clients", "validator", "validators",
    "beacon", "BeaconNode", "consensus", "CONSENSUS", "VULN", "LEAD",
    "vulnerability", "vulnerabilities", "implement", "implements",
    "implementation", "implementations", "different", "differently",
    "missing", "present", "absent", "explicit", "explicitly", "implicit",
    "transition", "transitions", "guard", "guards", "action", "actions",
    "workflow", "workflows", "deviating", "involved",
    # client names
    "prysm", "lighthouse", "grandine", "teku", "lodestar",
})


def _claim_keywords(diff: dict[str, Any]) -> set[str]:
    """Extract significant identifiers a supporting snippet must mention.

    Pulls CamelCase / snake_case / MACRO_CASE tokens of length ≥ 4 from
    the finding's ``transition_guard`` (highest signal), plus
    ``description`` and ``security_note``. Common English filler and
    domain-generic words are removed via :data:`_CLAIM_STOPWORDS`.
    """
    parts = [
        diff.get("transition_guard") or "",
        diff.get("description") or "",
        diff.get("security_note") or "",
    ]
    text = " ".join(parts)
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text)
    out: set[str] = set()
    for tok in raw:
        if tok in _CLAIM_STOPWORDS:
            continue
        if tok.lower() in _CLAIM_STOPWORDS:
            continue
        out.add(tok)
    return out


def _snippet_identifiers(snippet: str) -> set[str]:
    """Identifier-like tokens occurring in *snippet* (length ≥ 4)."""
    if not snippet:
        return set()
    return {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", snippet)
            if t not in _CLAIM_STOPWORDS}


def _grounding_hits(snippet: str, keywords: set[str]) -> tuple[int, list[str]]:
    """Return ``(hit_count, hit_list)`` — case-insensitive substring match."""
    if not snippet or not keywords:
        return 0, []
    s_lower = snippet.lower()
    hits = [k for k in keywords if k.lower() in s_lower]
    return len(hits), hits


def _client_language(client: str) -> str | None:
    try:
        lang_key, _ = LANGUAGE_GRAMMARS[client]
        return lang_key
    except KeyError:
        return None


def _normalize_lines(raw: Any) -> tuple[int, int]:
    """Coerce evidence ``lines`` (list/tuple/dict) into ``(start, end)``."""
    if raw is None:
        return 0, 0
    if isinstance(raw, dict):
        s = int(raw.get("start", raw.get("from", 0)) or 0)
        e = int(raw.get("end",   raw.get("to",   0)) or 0)
        return s, (e or s)
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 2:
            try:
                return int(raw[0] or 0), int(raw[1] or 0)
            except (TypeError, ValueError):
                return 0, 0
        if len(raw) == 1:
            try:
                v = int(raw[0] or 0)
                return v, v
            except (TypeError, ValueError):
                return 0, 0
    if isinstance(raw, int):
        return raw, raw
    return 0, 0


def validate_evidence(
    evidence: dict[str, Any] | None,
    client: str,
    *,
    claim_keywords: set[str] | None = None,
    min_grounding_hits: int = 1,
) -> EvidenceVerdict:
    """Validate one evidence block for *client*.

    *evidence* is the dict shape used throughout the pipeline::

        {"file": "...", "function": "...", "lines": [start, end]}

    When *claim_keywords* is provided, the cited snippet must contain at
    least *min_grounding_hits* of those tokens (case-insensitive
    substring). Findings whose snippet has zero overlap with the claim
    are dropped; findings with partial overlap are downgraded. This is
    the anti-stitching gate.
    """
    verdict = EvidenceVerdict()

    if not evidence:
        verdict.action = DROP
        verdict.is_production = False
        verdict.downgrade_reason = "no evidence provided"
        return verdict

    file_path = (evidence.get("file") or "").strip()
    if not file_path:
        verdict.action = DROP
        verdict.is_production = False
        verdict.downgrade_reason = "evidence missing 'file'"
        return verdict

    language = _client_language(client)

    # 1) Path-only test detection — cheap, runs before disk I/O.
    is_test, reason = is_test_path(file_path, language)
    if is_test:
        verdict.is_production = False
        verdict.in_test_block = True
        verdict.test_reason = reason
        verdict.action = DROP
        verdict.downgrade_reason = f"non-production path: {reason}"
        return verdict

    # 2) Line-range + file-existence check.
    start, end = _normalize_lines(evidence.get("lines"))
    if start <= 0:
        verdict.action = DROP
        verdict.downgrade_reason = "evidence missing valid line range"
        return verdict
    if end < start:
        end = start

    snippet, info = read_source_lines(client, file_path, start, end)
    verdict.file_exists   = info["file_exists"]
    verdict.resolved_path = info["resolved_path"]
    verdict.total_lines   = info["total_lines"]
    verdict.line_verified = info["line_verified"]

    if not verdict.file_exists:
        verdict.action = DROP
        verdict.is_production = False
        verdict.downgrade_reason = (
            f"file not found under code/{client}/: {file_path}"
        )
        return verdict

    if snippet is None or not verdict.line_verified:
        verdict.action = DOWNGRADE
        verdict.downgrade_reason = (
            f"line range L{start}-{end} out of file (total={info['total_lines']})"
        )
        return verdict

    verdict.excerpt = snippet

    # 3) In-file test block detection (`#[cfg(test)] mod tests`, etc.).
    in_test, t_reason = detect_test_block(
        text=_full_text_for(client, file_path) or snippet,
        start_line=start,
        end_line=end,
        language=language,
    )
    if in_test:
        verdict.is_production = False
        verdict.in_test_block = True
        verdict.test_reason = t_reason
        verdict.action = DROP
        verdict.downgrade_reason = f"non-production code: {t_reason}"
        return verdict

    # 4) Weak-invariant-only check (debug_assert! / expect("…") / panic!).
    if _is_weak_invariant_only(snippet):
        verdict.weak_invariant_only = True
        verdict.action = DOWNGRADE
        verdict.downgrade_reason = (
            "evidence is only a debug_assert!/expect()/panic! invariant — "
            "not a remote exploit primitive"
        )
        return verdict

    # 5) Claim-grounding (anti-stitching) — snippet must mention the
    #    finding it claims to support.
    if claim_keywords:
        verdict.claim_keywords_checked = len(claim_keywords)
        hits, hit_list = _grounding_hits(snippet, claim_keywords)
        verdict.claim_hits = hits
        verdict.snippet_identifiers = sorted(_snippet_identifiers(snippet))[:64]
        if hits == 0:
            verdict.claim_grounded = False
            verdict.action = DROP
            verdict.downgrade_reason = (
                "snippet does not mention any claim keyword "
                f"(checked {len(claim_keywords)} tokens from guard/description). "
                "Likely an unrelated source location stitched into the finding."
            )
            return verdict
        if hits < min_grounding_hits:
            verdict.claim_grounded = False
            verdict.action = DOWNGRADE
            verdict.downgrade_reason = (
                f"weak claim-grounding: only {hits} of "
                f"{len(claim_keywords)} claim tokens present in snippet "
                f"(required ≥ {min_grounding_hits})"
            )
            return verdict
        verdict.claim_grounded = True

    return verdict


def _is_weak_invariant_only(snippet: str) -> bool:
    """True if *snippet* contains an invariant macro and nothing more substantive."""
    if not snippet:
        return False
    has_weak = any(p.search(snippet) for p in _WEAK_INVARIANT_PATTERNS)
    if not has_weak:
        return False
    # Cheap structural floor: more than ~12 non-trivial lines means there's
    # likely real logic surrounding the assert; don't downgrade it.
    non_trivial = [
        ln for ln in snippet.splitlines()
        if ln.strip() and not ln.strip().startswith(("//", "#", "/*", "*"))
    ]
    return len(non_trivial) <= 12


def _full_text_for(client: str, file_path: str) -> str | None:
    # Lazy import to avoid cycles in unit tests.
    from tools.source_reader import get_full_text
    return get_full_text(client, file_path)


# ---------------------------------------------------------------------------
# Finding-level aggregation
# ---------------------------------------------------------------------------

def validate_finding(
    diff: dict[str, Any],
    *,
    require_exploit_chain: bool = True,
) -> FindingVerdict:
    """Validate a B/C-class finding's per-client evidence + structural fields.

    Aggregation rules
    -----------------
    * If *every* per-client evidence is ``DROP`` → finding ``DROP`` /
      ``evidence_quality = INVALID``.
    * If *any* per-client evidence is ``DROP`` and the finding is marked
      ``[CONSENSUS VULN]`` → ``DROP`` (vulnerability claims need clean evidence).
    * If any per-client evidence is ``DOWNGRADE`` → finding ``DOWNGRADE``,
      ``evidence_quality = WEAK``.
    * If ``require_exploit_chain`` and the finding has neither a non-empty
      ``exploit_chain`` nor a substantive ``security_note`` describing a
      reachable trigger → ``DOWNGRADE`` with reason ``missing exploit chain``.
    """
    fv = FindingVerdict()

    deviating = list(diff.get("deviating_clients", []) or [])
    involved  = list(diff.get("involved_clients",  []) or [])
    clients   = deviating or involved
    if not clients:
        fv.evidence_quality = "INVALID"
        fv.action = DROP
        fv.is_production = False
        fv.line_verified = False
        fv.downgrade_reasons.append("no involved clients on finding")
        return fv

    evidence_map = diff.get("evidence", {}) or {}

    # ── P1 hardening: claim-grounding ────────────────────────────────
    claim_keywords = _claim_keywords(diff)
    is_consensus_vuln = "[CONSENSUS VULN]" in (diff.get("security_note") or "")
    # Vulnerabilities require a stronger snippet ↔ claim overlap.
    min_hits = 2 if is_consensus_vuln else 1
    # If the claim itself yielded no significant identifiers (rare —
    # mostly happens when guard is "true"/"TRUE" and the description is
    # vague), we cannot ground anything; skip the gate to avoid false
    # drops. The exploit-chain check below still applies.
    if len(claim_keywords) < 2:
        claim_keywords = set()

    n_drop = 0
    n_downgrade = 0
    for client in clients:
        ev = evidence_map.get(client)
        ev_dict = ev if isinstance(ev, dict) else (
            ev.model_dump() if hasattr(ev, "model_dump") else None
        )
        per = validate_evidence(
            ev_dict, client,
            claim_keywords=claim_keywords or None,
            min_grounding_hits=min_hits,
        )
        fv.per_client[client] = per
        if per.action == DROP:
            n_drop += 1
            fv.downgrade_reasons.append(f"{client}: {per.downgrade_reason}")
        elif per.action == DOWNGRADE:
            n_downgrade += 1
            fv.downgrade_reasons.append(f"{client}: {per.downgrade_reason}")

    severity = (diff.get("severity") or "").upper()

    if n_drop == len(clients):
        fv.action = DROP
        fv.evidence_quality = "INVALID"
        fv.is_production = False
        fv.line_verified = False
        return fv

    if n_drop > 0 and is_consensus_vuln:
        fv.action = DROP
        fv.evidence_quality = "INVALID"
        fv.is_production = False
        fv.line_verified = any(p.line_verified for p in fv.per_client.values())
        fv.downgrade_reasons.append(
            "[CONSENSUS VULN] cannot rely on partially non-production evidence"
        )
        return fv

    fv.is_production = all(p.is_production for p in fv.per_client.values()
                           if p.action != DROP)
    fv.line_verified = all(p.line_verified for p in fv.per_client.values()
                           if p.action != DROP)

    if n_downgrade > 0 or n_drop > 0:
        fv.action = DOWNGRADE
        fv.evidence_quality = "WEAK"

    # ── P1 hardening: anti-stitching ────────────────────────────────
    # For consensus-vuln claims with multiple supporting snippets, the
    # snippets must share at least one significant identifier — otherwise
    # they describe unrelated code paths that the LLM concatenated into
    # a fake exploit chain.
    if is_consensus_vuln:
        kept = [
            (c, p) for c, p in fv.per_client.items()
            if p.action != DROP and p.snippet_identifiers
        ]
        if len(kept) >= 2:
            common: set[str] | None = None
            for _, per in kept:
                ids = set(per.snippet_identifiers)
                common = ids if common is None else (common & ids)
                if not common:
                    break
            if not common:
                fv.action = DROP
                fv.evidence_quality = "INVALID"
                fv.is_production = False
                fv.downgrade_reasons.append(
                    "anti-stitching: per-client snippets share no common "
                    "identifier — evidence chain looks fabricated from "
                    "unrelated source locations"
                )
                return fv

    # Exploit-chain check — only enforce on MAJOR/CRITICAL.
    if require_exploit_chain and severity in ("MAJOR", "CRITICAL"):
        chain = diff.get("exploit_chain") or []
        sec_note = (diff.get("security_note") or "").strip()
        has_chain = bool(chain) or _looks_like_exploit_chain(sec_note)
        if not has_chain:
            fv.action = DOWNGRADE if fv.action != DROP else fv.action
            if fv.evidence_quality == "STRONG":
                fv.evidence_quality = "WEAK"
            fv.downgrade_reasons.append(
                "missing exploit_chain (no attacker / trigger / impact described)"
            )

    return fv


# A "real" exploit chain mentions at least an attacker, a trigger and an impact.
_RE_ATTACKER = re.compile(
    r"\b(remote\s+peer|malicious\s+(?:peer|validator|builder|el|node)|"
    r"adversary|attacker|byzantine|equivocat|colluding|byzantine\s+set)\b",
    re.I,
)
_RE_TRIGGER = re.compile(
    r"\b(send|gossip|broadcast|deliver|race|delay|withhold|replay|craft|forge|"
    r"submit|publish|inject|stall|spam|flood)\b",
    re.I,
)
_RE_IMPACT = re.compile(
    r"\b(crash|panic|stall|deadlock|fork|finality|safety|liveness|slash|"
    r"reorg|oom|memory\s+exhaust|cpu\s+exhaust|halt|split)\b",
    re.I,
)


def _looks_like_exploit_chain(text: str) -> bool:
    if not text or len(text) < 40:
        return False
    return bool(
        _RE_ATTACKER.search(text)
        and _RE_TRIGGER.search(text)
        and _RE_IMPACT.search(text)
    )

