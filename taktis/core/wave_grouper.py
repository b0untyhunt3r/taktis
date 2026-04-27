"""DAG-based wave auto-assignment for task dependencies.

Extracted from :mod:`scheduler` to keep scheduling orchestration separate
from the pure DAG algorithm.  The :func:`auto_assign_waves` function is
the only public entry point.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


def auto_assign_waves(tasks: list[dict]) -> dict[int, list[dict]]:
    """Auto-assign wave numbers based on the dependency DAG.

    Parameters
    ----------
    tasks:
        A list of dicts, each having at least ``"id"`` and ``"depends_on"``
        (a list of task IDs).

    Returns
    -------
    dict[int, list[dict]]
        Mapping of wave number (starting at 1) to the tasks in that wave.
        Each task dict is mutated to include a ``"wave"`` key.
    """
    task_map: dict[str, dict] = {t["id"]: t for t in tasks}
    assigned: dict[str, int] = {}

    def _wave_of(tid: str, visited: set[str] | None = None) -> int:
        if tid in assigned:
            return assigned[tid]
        if visited is None:
            visited = set()
        if tid in visited:
            logger.warning("Cycle detected involving task %s – breaking at wave 1", tid)
            return 1
        visited.add(tid)
        task = task_map.get(tid)
        if task is None:
            return 1
        deps = task.get("depends_on") or []
        # depends_on may be a JSON string from the DB
        if isinstance(deps, str):
            deps = json.loads(deps)
        if not deps:
            assigned[tid] = 1
            return 1
        max_dep_wave = max(
            (_wave_of(d, visited) for d in deps if d in task_map),
            default=0,
        )
        wave = max_dep_wave + 1
        assigned[tid] = wave
        return wave

    for t in tasks:
        _wave_of(t["id"])

    waves: dict[int, list[dict]] = defaultdict(list)
    for t in tasks:
        w = assigned.get(t["id"], 1)
        t["wave"] = w
        waves[w].append(t)
    return dict(waves)
