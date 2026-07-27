"""Shared Jinja template loader for all agents.

Collapses the 6 duplicated ``Template(Path(...).read_text())`` loaders into one
cached helper. Accepts either a bare filename (``"phase2_sub.j2"``) or an
absolute/relative path, preserving both call styles used across the codebase.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Template

_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompts"
_cache: dict[str, Template] = {}


def load_template(name_or_path: str | Path) -> Template:
    """Load (and cache) a Jinja template.

    *name_or_path* may be a bare filename resolved against ``prompts/`` or a
    concrete path (absolute, or relative to cwd). Cached by its string form.
    """
    key = str(name_or_path)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    p = Path(name_or_path)
    if not p.is_absolute() and not p.exists():
        p = _TEMPLATE_DIR / name_or_path
    tmpl = Template(p.read_text(encoding="utf-8"))
    _cache[key] = tmpl
    return tmpl
