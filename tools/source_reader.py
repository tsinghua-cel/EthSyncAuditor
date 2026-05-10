"""Source-file reader and test-context detector.

Used by ``tools.evidence_validator`` and ``agents.phase3_verify_agent`` to
re-read the actual production source code referenced by a finding's
``evidence`` block. This is the *physical* foundation that makes claims
like "evidence is in `#[cfg(test)] mod tests`" or "line range out of
file" decidable rather than left to the LLM.

Resolution rules
----------------
* The ``file`` field of an evidence block is treated as a path *relative
  to* ``code/<client>/``. Absolute paths are tolerated but discouraged.
* If the file cannot be located, ``read_source_lines`` returns ``None``
  and the caller MUST treat the evidence as unverifiable.

Test-context detection
----------------------
Pure regex / path heuristics — fast, deterministic, no AST. The goal is
*recall*, not parser-grade precision: a false positive only downgrades a
finding, it never invents one.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

from config import CODE_BASE_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path-level test-file detection (per language)
# ---------------------------------------------------------------------------

# Path fragments that universally indicate non-production code.
_GENERIC_TEST_DIR_MARKERS = (
    "/tests/",
    "/test/",
    "/__tests__/",
    "/spec/",
    "/specs/",
    "/fixtures/",
    "/fixture/",
    "/mocks/",
    "/mock/",
    "/testing/",
    "/testdata/",
    "/test_data/",
    "/test-utils/",
    "/testutil/",
    "/test_helpers/",
    "/benches/",
    "/bench/",
    "/examples/",
)

# Per-extension filename suffix rules.
_FILENAME_SUFFIX_RULES: dict[str, tuple[str, ...]] = {
    ".go":   ("_test.go",),
    ".rs":   ("_tests.rs",),
    ".ts":   (".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx"),
    ".tsx":  (".test.tsx", ".spec.tsx"),
    ".js":   (".test.js", ".spec.js"),
    ".java": ("Test.java", "IT.java", "Tests.java"),
}

# Language-specific path markers (extra over the generic set).
_LANG_PATH_MARKERS: dict[str, tuple[str, ...]] = {
    "rust":       ("/src/bin/test_generator", "/test_generator/", "/tests/"),
    "go":         ("/testutil/", "/mock/", "/mocks/"),
    "java":       ("/src/test/", "/integration-test/"),
    "typescript": ("/__tests__/", "/test/"),
}


def is_test_path(file_path: str, language: str | None = None) -> tuple[bool, str]:
    """Return ``(is_test, reason)`` for *file_path*.

    ``language`` is one of ``"go" | "rust" | "java" | "typescript"`` (case
    insensitive); if omitted the function only applies generic rules.
    """
    if not file_path:
        return False, ""

    lower = file_path.lower().replace("\\", "/")

    for marker in _GENERIC_TEST_DIR_MARKERS:
        if marker in lower:
            return True, f"path contains '{marker.strip('/')}'"

    if language:
        lang = language.lower()
        for marker in _LANG_PATH_MARKERS.get(lang, ()):
            if marker.lower() in lower:
                return True, f"path contains '{marker.strip('/')}' ({lang})"

    fname = Path(file_path).name
    for ext, suffixes in _FILENAME_SUFFIX_RULES.items():
        if fname.endswith(ext):
            for suf in suffixes:
                if fname.endswith(suf):
                    return True, f"filename ends with '{suf}'"

    # Common pattern: path explicitly contains the word 'test_generator'
    if "test_generator" in lower:
        return True, "path contains 'test_generator'"

    return False, ""


# ---------------------------------------------------------------------------
# In-file test-block detection (Rust / TS / Go / Java)
# ---------------------------------------------------------------------------

# Rust: lines that *open* a cfg(test) module or carry #[cfg(test)] / #[test].
_RE_RUST_CFG_TEST_ATTR = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]")
_RE_RUST_TEST_ATTR     = re.compile(r"#\s*\[\s*(?:tokio::)?test(?:\s*\(.*\))?\s*\]")
_RE_RUST_MOD_TESTS     = re.compile(r"^\s*(?:pub\s+)?mod\s+tests?\s*\{")

# TS/JS: describe(/it(/test( blocks.
_RE_TS_TEST_BLOCK = re.compile(r"^\s*(?:describe|it|test)\s*\(")

# Go: TestXxx / BenchmarkXxx funcs.
_RE_GO_TEST_FUNC  = re.compile(r"^\s*func\s+(?:Test|Benchmark|Fuzz|Example)[A-Z]\w*\s*\(")

# Java: JUnit annotations.
_RE_JAVA_TEST_ANNOT = re.compile(r"^\s*@(?:Test|ParameterizedTest|RepeatedTest|TestFactory)\b")


def _compute_rust_cfg_test_ranges(text: str) -> list[tuple[int, int]]:
    """Return inclusive 1-based line ranges enclosed by ``#[cfg(test)] mod`` blocks.

    A simple brace-matching scanner. Handles the common pattern::

        #[cfg(test)]
        mod tests {
            // ... test code ...
        }

    Any preceding ``#[cfg(test)]`` attribute attaches to the next ``mod`` /
    ``fn`` block.
    """
    lines = text.splitlines()
    pending_cfg = False
    ranges: list[tuple[int, int]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if _RE_RUST_CFG_TEST_ATTR.search(stripped):
            pending_cfg = True
            i += 1
            continue

        if pending_cfg and ("mod " in stripped or stripped.startswith("fn ")
                            or "fn " in stripped):
            # Find the opening brace position (could be on same or next line).
            depth = 0
            start = i + 1  # 1-based
            j = i
            opened = False
            while j < len(lines):
                for ch in lines[j]:
                    if ch == "{":
                        depth += 1
                        opened = True
                    elif ch == "}":
                        depth -= 1
                        if opened and depth == 0:
                            ranges.append((start, j + 1))
                            j += 1
                            break
                else:
                    j += 1
                    continue
                if opened and depth == 0:
                    break
            pending_cfg = False
            i = max(j, i + 1)
            continue

        # Reset pending flag if we see something that isn't a comment/attribute.
        if pending_cfg and stripped and not stripped.startswith(("//", "#[")):
            pending_cfg = False

        i += 1

    return ranges


def detect_test_block(
    text: str,
    start_line: int,
    end_line: int,
    language: str | None,
) -> tuple[bool, str]:
    """Return ``(in_test, reason)`` for the ``[start_line, end_line]`` slice."""
    if not text or start_line <= 0 or end_line < start_line:
        return False, ""

    lines = text.splitlines()
    if start_line > len(lines):
        return False, ""
    end_clamped = min(end_line, len(lines))
    snippet_lines = lines[start_line - 1:end_clamped]
    snippet = "\n".join(snippet_lines)
    lang = (language or "").lower()

    if lang == "rust":
        # 1) The slice itself contains an in-band #[test] / #[cfg(test)].
        for ln in snippet_lines:
            s = ln.strip()
            if _RE_RUST_TEST_ATTR.search(s) or _RE_RUST_CFG_TEST_ATTR.search(s):
                return True, "Rust #[test] / #[cfg(test)] inside slice"
            if _RE_RUST_MOD_TESTS.match(s):
                return True, "Rust 'mod tests {' inside slice"
        # 2) The slice falls inside a file-level cfg(test) mod block.
        for lo, hi in _compute_rust_cfg_test_ranges(text):
            if start_line >= lo and end_clamped <= hi:
                return True, f"Rust slice inside #[cfg(test)] mod block (L{lo}-L{hi})"

    elif lang == "go":
        for ln in snippet_lines:
            if _RE_GO_TEST_FUNC.search(ln):
                return True, "Go Test*/Benchmark*/Fuzz*/Example* function"

    elif lang in ("typescript", "javascript", "ts", "js"):
        for ln in snippet_lines:
            if _RE_TS_TEST_BLOCK.search(ln):
                return True, "TS/JS describe()/it()/test() block"

    elif lang == "java":
        for ln in snippet_lines:
            if _RE_JAVA_TEST_ANNOT.search(ln):
                return True, "Java JUnit @Test annotation"

    # Generic: explicit assert/expect-only "invariant" code is NOT test, but
    # callers may still want to treat it as weak evidence — handled elsewhere.
    return False, ""


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def _resolve_path(client: str, file_path: str) -> Path | None:
    """Resolve *file_path* to an absolute path under ``code/<client>/``.

    Returns ``None`` if the path cannot be located. Tolerates leading
    ``code/<client>/`` prefixes that some upstream prompts emit verbatim.
    """
    if not file_path:
        return None

    p = Path(file_path)
    if p.is_absolute() and p.exists():
        return p

    base = CODE_BASE_PATH / client
    candidate = (base / file_path).resolve()
    if candidate.exists():
        return candidate

    # Strip a leading ``code/<client>/`` if the prompt re-emitted it.
    parts = Path(file_path).parts
    if len(parts) >= 2 and parts[0] in ("code",) and parts[1] == client:
        candidate = (base / Path(*parts[2:])).resolve()
        if candidate.exists():
            return candidate

    # Last resort: shallow scan by basename (single match only).
    if base.exists():
        matches = list(base.rglob(Path(file_path).name))
        if len(matches) == 1:
            return matches[0]

    return None


@lru_cache(maxsize=512)
def _read_text_cached(path_str: str) -> str | None:
    try:
        return Path(path_str).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_source_lines(
    client: str,
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    max_lines: int = 200,
) -> tuple[str | None, dict]:
    """Read ``[start_line, end_line]`` of a client source file.

    Returns ``(snippet_or_None, info)`` where ``info`` contains:

    * ``file_exists``    — bool
    * ``resolved_path``  — absolute path str or ``""``
    * ``total_lines``    — int (0 if missing)
    * ``line_verified``  — both bounds within ``[1, total_lines]``
    """
    info = {
        "file_exists":    False,
        "resolved_path":  "",
        "total_lines":    0,
        "line_verified":  False,
    }

    resolved = _resolve_path(client, file_path)
    if resolved is None:
        return None, info

    info["file_exists"] = True
    info["resolved_path"] = str(resolved)

    text = _read_text_cached(str(resolved))
    if text is None:
        return None, info

    lines = text.splitlines()
    info["total_lines"] = len(lines)

    if start_line <= 0 or end_line < start_line:
        return None, info
    if start_line > len(lines):
        return None, info

    end_clamped = min(end_line, len(lines), start_line + max_lines - 1)
    info["line_verified"] = (end_line <= len(lines))

    snippet = "\n".join(lines[start_line - 1:end_clamped])
    return snippet, info


def get_full_text(client: str, file_path: str) -> str | None:
    """Return the full text of a file under ``code/<client>/`` or ``None``."""
    resolved = _resolve_path(client, file_path)
    if resolved is None:
        return None
    return _read_text_cached(str(resolved))

