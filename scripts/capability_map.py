"""Capability task-map governance (issue #53).

The capability manifest (``capabilities/manifest.json``) declares every
capability record and its state (supported / supported-with-guard /
unsupported-by-design / unknown).  The capability task map
(``capabilities/task_map.json``, schema ``docx2typed-capability-task-map-1``)
closes the traceability contract:

- every manifest capability id maps to an explicit case set: release task
  ids (``capabilities/tasks/*.json``), agent task ids (``agent.json``),
  metamorphic relation ids (``scripts/release_acceptance.py``), and inline
  matrix cases (executed by the ``capability_matrix`` qualification check);
- every case id traces back to exactly one capability;
- the frozen state counts are enforced (26 supported / 5 guarded /
  10 stable-negative / 0 unknown) and task count is never assumed to equal
  capability count;
- every guard capability declares allowed+refused boundaries; every
  stable-negative capability declares an exact Diagnostic code with a
  no-forbidden-side-effect proof;
- the failure catalog lists the mutation-path Diagnostic codes with their
  cases; every listed code is registered in the protocol schema bundle and
  every listed code has at least one case.

The module is pure validation (no execution): the ``capability_matrix``
qualification check in ``scripts/qualify.py`` executes the inline matrix
cases through the capture-only adapters.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TASK_MAP_SCHEMA = "docx2typed-capability-task-map-1"
CASE_KINDS = ("task", "agent", "metamorphic", "matrix")
SET_KEYS = ("tasks", "agents", "metamorphic", "matrix")
PROBE_KINDS = ("positive", "guard-allowed", "guard-refused", "stable-negative", "negative")
KNOWN_STATES = ("supported", "supported-with-guard", "unsupported-by-design")


class TaskMapError(ValueError):
    """The capability task map (or its validation) is structurally invalid."""


def load_task_map(root: Path) -> dict[str, Any]:
    path = root / "capabilities" / "task_map.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskMapError(f"unreadable task map {path}: {exc}") from exc
    if data.get("schema") != TASK_MAP_SCHEMA:
        raise TaskMapError(f"task map schema {data.get('schema')!r} != {TASK_MAP_SCHEMA}")
    return data


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "capabilities" / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskMapError(f"unreadable capability manifest {path}: {exc}") from exc
    if data.get("schema") != "docx2typed-capability-manifest-1":
        raise TaskMapError(f"manifest schema {data.get('schema')!r} is not the frozen manifest schema")
    return data


def _task_ids(root: Path, file_name: str) -> list[str]:
    path = root / "capabilities" / "tasks" / file_name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskMapError(f"unreadable {path}: {exc}") from exc
    return [entry["id"] for entry in data.get("tasks", []) if isinstance(entry, dict)]


def release_task_ids(root: Path) -> list[str]:
    """Every release task id across capabilities/tasks/*.json (agent.json
    excluded: agent prompts run via agent_bench, not the release suite)."""
    ids: list[str] = []
    for path in sorted((root / "capabilities" / "tasks").glob("*.json")):
        if path.name == "agent.json":
            continue
        ids.extend(_task_ids(root, path.name))
    return ids


def agent_task_ids(root: Path) -> list[str]:
    return _task_ids(root, "agent.json")


def metamorphic_cases() -> list[tuple[str, str]]:
    """(id, capability) for every metamorphic relation executed by the
    release-acceptance runner; the source of truth is the runner itself."""
    from scripts.release_acceptance import METAMORPHIC_CASES

    return list(METAMORPHIC_CASES)


def validate_task_map(
    root: Path,
    bundle: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    task_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structural validation of the committed task map against the frozen
    manifest.  Returns ``{valid, detail, counts, cases}``; any drift raises
    nothing but fails the returned verdict (the qualification check turns a
    failed verdict into a failed check, never a pass)."""
    manifest = manifest if manifest is not None else load_manifest(root)
    task_map = task_map if task_map is not None else load_task_map(root)
    if bundle is None:
        from scripts.protocol import schema_bundle

        bundle = schema_bundle()
    registered = set(bundle.get("diagnostics", {}))

    problems: list[str] = []
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list):
        problems.append("manifest.capabilities is not a list")
        capabilities = []

    manifest_by_id: dict[str, dict[str, Any]] = {}
    for entry in capabilities:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            problems.append("manifest capability without an id")
            continue
        manifest_by_id[entry["id"]] = entry

    state_counts = {"supported": 0, "supported-with-guard": 0, "unsupported-by-design": 0}
    for entry in capabilities:
        state = entry.get("state")
        if state not in KNOWN_STATES:
            problems.append(f"capability {entry.get('id')}: unknown state {state!r}")
        elif state in state_counts:
            state_counts[state] += 1
    unknown = manifest.get("unknown") or []
    if unknown:
        problems.append(f"manifest declares unknown capabilities: {unknown}")
    state_counts["unknown"] = len(unknown)

    mapped = task_map.get("capabilities")
    cases = task_map.get("cases")
    if not isinstance(mapped, dict) or not isinstance(cases, dict):
        problems.append("task_map needs capabilities and cases objects")
        return {
            "valid": False,
            "detail": "; ".join(problems) or "ok",
            "counts": {},
            "cases": {},
        }

    # 1. every manifest capability has a map entry with the same state
    for cap_id in manifest_by_id:
        entry = mapped.get(cap_id)
        if entry is None:
            problems.append(f"capability {cap_id} missing from task_map.capabilities")
            continue
        if entry.get("state") != manifest_by_id[cap_id].get("state"):
            problems.append(
                f"capability {cap_id}: map state {entry.get('state')!r} != manifest state "
                f"{manifest_by_id[cap_id].get('state')!r}"
            )

    # 2. every case id resolves and traces back to exactly one capability
    referenced: dict[str, str] = {}
    for cap_id, entry in mapped.items():
        if cap_id not in manifest_by_id:
            problems.append(f"task_map capability {cap_id} not in the manifest")
            continue
        case_sets = entry.get("cases") if isinstance(entry, dict) else None
        if not isinstance(case_sets, dict):
            problems.append(f"capability {cap_id}: cases must be an object")
            continue
        for kind in SET_KEYS:
            for case_id in case_sets.get(kind, []):
                if case_id in referenced:
                    problems.append(f"case {case_id} referenced by both {referenced[case_id]} and {cap_id}")
                else:
                    referenced[case_id] = cap_id
    for case_id in referenced:
        if case_id not in cases:
            problems.append(f"case {case_id} referenced by a capability but missing from task_map.cases")
    for case_id, spec in cases.items():
        if case_id not in referenced:
            problems.append(f"case {case_id} is declared but referenced by no capability")
            continue
        if spec.get("capability") != referenced[case_id]:
            problems.append(
                f"case {case_id}: traces to {referenced[case_id]} but declares capability {spec.get('capability')!r}"
            )
        if spec.get("kind") not in CASE_KINDS:
            problems.append(f"case {case_id}: unknown kind {spec.get('kind')!r}")

    # 3. every release/agent task id appears exactly once
    task_ids = release_task_ids(root)
    agent_ids = agent_task_ids(root)
    for task_id in task_ids:
        if task_id not in referenced:
            problems.append(f"release task {task_id} has no task_map case")
    for agent_id in agent_ids:
        if agent_id not in referenced:
            problems.append(f"agent task {agent_id} has no task_map case")
    for kind, ids in (("task", task_ids), ("agent", agent_ids)):
        for case_id, cap in referenced.items():
            spec = cases.get(case_id, {})
            if spec.get("kind") == kind and case_id not in ids:
                problems.append(f"case {case_id} declares kind {kind} but is not a {kind} id")

    # 4. metamorphic ids match the release runner
    meta_by_id = {case_id: cap for case_id, cap in metamorphic_cases()}
    for case_id, cap in referenced.items():
        if cases.get(case_id, {}).get("kind") != "metamorphic":
            continue
        if case_id not in meta_by_id:
            problems.append(f"metamorphic case {case_id} not registered by release_acceptance")
        elif meta_by_id[case_id] != cases[case_id].get("capability"):
            problems.append(
                f"metamorphic case {case_id}: runner maps to {meta_by_id[case_id]} "
                f"but the map declares {cases[case_id].get('capability')!r}"
            )
    for case_id, cap in meta_by_id.items():
        if case_id not in referenced:
            problems.append(f"runner metamorphic {case_id} missing from the task map")

    # 5. every capability's case set is non-empty and state-appropriate
    matrix_cases = task_map.get("matrix_cases", {})
    for cap_id, entry in mapped.items():
        if cap_id not in manifest_by_id:
            continue
        state = manifest_by_id[cap_id].get("state")
        case_sets = entry.get("cases", {})
        total = sum(len(case_sets.get(kind, [])) for kind in SET_KEYS)
        if total == 0:
            problems.append(f"capability {cap_id}: empty case set")
        if state == "unsupported-by-design":
            negative = [
                cid for cid in case_sets.get("matrix", [])
                if matrix_cases.get(cid, {}).get("probe") in ("stable-negative", "negative")
                and matrix_cases.get(cid, {}).get("diagnostic")
            ]
            if not negative:
                problems.append(
                    f"capability {cap_id}: unsupported-by-design needs a stable-negative matrix case "
                    "with an exact diagnostic"
                )
        if state == "supported-with-guard":
            allowed = [
                cid for cid in case_sets.get("matrix", [])
                if matrix_cases.get(cid, {}).get("probe") in ("positive", "guard-allowed")
            ]
            refused = [
                cid for cid in case_sets.get("matrix", [])
                if matrix_cases.get(cid, {}).get("probe") in ("guard-refused", "stable-negative")
            ]
            task_allowed = [
                cid for cid in case_sets.get("tasks", [])
                if _task_has_success_steps(root, cid)
            ]
            task_refused = [
                cid for cid in case_sets.get("tasks", [])
                if _task_has_fail_steps(root, cid)
            ]
            if not (allowed or task_allowed):
                problems.append(f"capability {cap_id}: guard needs an allowed-boundary case")
            if not (refused or task_refused):
                problems.append(f"capability {cap_id}: guard needs a refused-boundary case")

    # 6. matrix case integrity
    for case_id, spec in matrix_cases.items():
        if case_id not in referenced:
            problems.append(f"matrix case {case_id} is defined but not referenced by any capability")
            continue
        if not isinstance(spec, dict):
            problems.append(f"matrix case {case_id}: must be an object")
            continue
        if spec.get("run") is False:
            if not spec.get("evidence"):
                problems.append(f"matrix case {case_id}: run:false needs evidence naming the unit test")
            if spec.get("probe") not in PROBE_KINDS:
                problems.append(f"matrix case {case_id}: unknown probe kind {spec.get('probe')!r}")
            continue
        if not isinstance(spec.get("ops"), list):
            problems.append(f"matrix case {case_id}: needs an ops list")
        elif not spec["ops"] and not spec.get("checks"):
            problems.append(f"matrix case {case_id}: needs ops or postcondition checks")
        probe = spec.get("probe")
        if probe not in PROBE_KINDS:
            problems.append(f"matrix case {case_id}: unknown probe kind {probe!r}")
        if probe in ("guard-refused", "stable-negative", "negative"):
            diagnostic = spec.get("diagnostic")
            if not isinstance(diagnostic, str):
                problems.append(f"matrix case {case_id}: {probe} needs an exact diagnostic")
            elif diagnostic not in registered:
                problems.append(f"matrix case {case_id}: diagnostic {diagnostic!r} not registered in the bundle")
            if spec.get("run", True) and not spec.get("no_mutation") and not spec.get("no_output"):
                problems.append(
                    f"matrix case {case_id}: {probe} needs a side-effect proof "
                    "(no_mutation and/or no_output)"
                )
        elif spec.get("diagnostic") is not None and spec["diagnostic"] not in registered:
            problems.append(f"matrix case {case_id}: diagnostic {spec['diagnostic']!r} not registered in the bundle")

    # 7. failure catalog: registered codes, each with a resolvable case
    catalog = task_map.get("failure_catalog", {})
    if not isinstance(catalog, dict):
        problems.append("task_map.failure_catalog must be an object")
    else:
        for code, entry in catalog.items():
            if code not in registered:
                problems.append(f"failure catalog code {code!r} not registered in the bundle")
                continue
            case_refs = (entry or {}).get("cases", [])
            if not isinstance(case_refs, list) or not case_refs:
                problems.append(f"failure catalog code {code}: no cases declared")
                continue
            for case_id in case_refs:
                if case_id not in referenced:
                    problems.append(f"failure catalog code {code}: case {case_id} does not exist")

    # 8. summary counts
    summary = task_map.get("summary", {})
    if not isinstance(summary, dict):
        problems.append("task_map.summary missing")
    else:
        counts = summary.get("counts", {})
        if counts != state_counts:
            problems.append(
                f"summary counts {counts} != manifest counts {state_counts}"
            )
        if summary.get("capability_count") != len(manifest_by_id):
            problems.append("summary.capability_count != manifest capability count")
        if summary.get("task_count") != len(task_ids):
            problems.append(f"summary.task_count != release task count ({len(task_ids)})")
        if summary.get("agent_count") != len(agent_ids):
            problems.append(f"summary.agent_count != agent task count ({len(agent_ids)})")
        if summary.get("metamorphic_count") != len(meta_by_id):
            problems.append(f"summary.metamorphic_count != runner count ({len(meta_by_id)})")
        if summary.get("matrix_count") != len(matrix_cases):
            problems.append(f"summary.matrix_count != declared matrix case count ({len(matrix_cases)})")
        if len(task_ids) == len(manifest_by_id):
            problems.append("task count must never be assumed equal to capability count")
        if summary.get("counts", {}).get("unknown", -1) != 0:
            problems.append("unknown capability count must be zero")

    counts = {
        "capability_count": len(manifest_by_id),
        "task_count": len(task_ids),
        "agent_count": len(agent_ids),
        "metamorphic_count": len(meta_by_id),
        "matrix_count": len(matrix_cases),
        **state_counts,
        "unknown": len(unknown),
    }
    return {
        "valid": not problems,
        "detail": "; ".join(problems) if problems else "task map matches the frozen manifest",
        "counts": counts,
        "cases": referenced,
    }


def _task_has_success_steps(root: Path, task_id: str) -> bool:
    for path in sorted((root / "capabilities" / "tasks").glob("*.json")):
        if path.name == "agent.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for task in data.get("tasks", []):
            if task.get("id") == task_id:
                return any(step.get("expect") == "ok" for step in task.get("steps", []))
    return False


def _task_has_fail_steps(root: Path, task_id: str) -> bool:
    for path in sorted((root / "capabilities" / "tasks").glob("*.json")):
        if path.name == "agent.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for task in data.get("tasks", []):
            if task.get("id") == task_id:
                return any(step.get("expect") == "fail" for step in task.get("steps", []))
    return False
