"""ferry.engine.waves - dependency-aware wave computation."""

from __future__ import annotations

from collections import defaultdict, deque


def topo_waves(tables: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Kahn-style wave grouping.

    edges[child] = set of parent tables (load parents first).
    Self-references are ignored; multi-table cycles raise ValueError with the stuck list
    so the plan can break the cycle explicitly.
    """
    all_tables = set(tables) | set(edges)
    deps: dict[str, set[str]] = defaultdict(set)
    for child, parents in edges.items():
        deps[child] |= {p for p in parents if p != child} & all_tables

    indeg = {t: len(deps[t]) for t in all_tables}
    parents_of: dict[str, set[str]] = defaultdict(set)
    for child, parents in deps.items():
        for p in parents:
            parents_of[p].add(child)

    ready = deque(sorted(t for t in all_tables if indeg[t] == 0))
    waves: list[list[str]] = []
    while ready:
        wave = sorted(ready)
        ready.clear()
        for t in wave:
            for child in sorted(parents_of[t]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    ready.append(child)
        waves.append(wave)

    if sum(len(w) for w in waves) < len(all_tables):
        stuck = sorted(t for t in all_tables if indeg[t] > 0)
        raise ValueError(
            "FK dependency cycle detected between: " + ", ".join(stuck) +
            ". Break the cycle in the plan (explicit `order:` override, or drop/defer those FKs)."
        )
    return waves


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def mapping_edges(mappings: list[dict]) -> dict[str, set[str]]:
    """FK edges between mapping targets, from each mapping's _target_fks evidence
    (populated at plan time from the target inspector). Schema-qualified references
    match exactly; an unqualified reference is used only when its table name is unique."""
    targets = [m["target"] for m in mappings]
    by_short: dict[str, list[str]] = defaultdict(list)
    for t in targets:
        by_short[_short(t)].append(t)
    edges: dict[str, set[str]] = {}
    for m in mappings:
        deps: set[str] = set()
        for fk in m.get("_target_fks", []):
            ref = fk.get("ref_table") or ""
            if ref in targets:
                dep = ref
            else:
                candidates = by_short.get(_short(ref), [])
                dep = candidates[0] if len(candidates) == 1 else None
            if dep and dep != m["target"]:
                deps.add(dep)
        edges[m["target"]] = deps
    return edges


def plan_waves(mappings: list[dict]) -> tuple[list[list[str]], dict[str, dict]]:
    """Waves of mapping dicts in dependency order. Returns (waves, by_target)."""
    by_target = {m["target"]: m for m in mappings}
    waves = topo_waves([m["target"] for m in mappings], mapping_edges(mappings))
    ordered = [[by_target[t] for t in wave] for wave in waves]
    return ordered, by_target
