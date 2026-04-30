"""Checkpoint persistence.

Save / load GlobalState snapshots for resumable runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config
from utils import safe_serialize

logger = logging.getLogger(__name__)


def save_checkpoint(state: dict[str, Any], phase: int, iteration: int) -> Path:
    """Serialize *state* to a checkpoint JSON file.

    Returns the path of the written file.
    """
    config.CHECKPOINT_PATH.mkdir(parents=True, exist_ok=True)
    filename = f"checkpoint_phase{phase}_iter{iteration}.json"
    path = config.CHECKPOINT_PATH / filename

    # Make state JSON-serializable (drop non-serializable objects)
    serializable = safe_serialize(state)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)

    logger.info("[save_checkpoint] phase=%d iter=%d → %s", phase, iteration, path)
    return path


def load_checkpoint(phase: int, iteration: int) -> dict[str, Any]:
    """Load a previously saved checkpoint.

    Raises FileNotFoundError if the checkpoint does not exist.
    """
    filename = f"checkpoint_phase{phase}_iter{iteration}.json"
    path = config.CHECKPOINT_PATH / filename
    if not path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {path}")

    with open(path, encoding="utf-8") as f:
        state = json.load(f)

    logger.info("[load_checkpoint] phase=%d iter=%d ← %s", phase, iteration, path)
    return state


def _parse_checkpoint_filename(path: Path) -> tuple[int, int]:
    """Extract (phase, iteration) from a checkpoint filename."""
    stem = path.stem  # e.g. "checkpoint_phase2_iter5"
    parts = stem.split("_")
    phase = int(parts[1].replace("phase", ""))
    iteration = int(parts[2].replace("iter", ""))
    return phase, iteration


def list_checkpoints() -> list[tuple[int, int, Path]]:
    """Return all available checkpoints sorted by (phase, iteration).

    Returns a list of (phase, iteration, path) tuples.
    """
    config.CHECKPOINT_PATH.mkdir(parents=True, exist_ok=True)
    results: list[tuple[int, int, Path]] = []
    for p in config.CHECKPOINT_PATH.glob("checkpoint_phase*_iter*.json"):
        try:
            phase, iteration = _parse_checkpoint_filename(p)
            results.append((phase, iteration, p))
        except (ValueError, IndexError):
            logger.warning("[list_checkpoints] skipping malformed file: %s", p)
    results.sort(key=lambda t: (t[0], t[1]))
    return results


def latest_checkpoint() -> tuple[int, int, dict[str, Any]] | None:
    """Find and load the latest checkpoint (highest phase, then iteration).

    Returns (phase, iteration, state) or None if no checkpoints exist.
    """
    ckpts = list_checkpoints()
    if not ckpts:
        return None

    phase, iteration, _path = ckpts[-1]
    state = load_checkpoint(phase, iteration)
    return phase, iteration, state
