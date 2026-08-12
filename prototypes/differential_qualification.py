"""THROWAWAY PROTOTYPE: implementation-independent qualification seam.

The harness owns task selection and comparison. A small implementation-specific
adapter owns only translation to the product's public CLI/MCP surface:

  plan    -> immutable request.json
  capture -> ADAPTER run request.json bundle.json
  compare -> oracle bundle vs candidate bundle

Try: python prototypes/differential_qualification.py self-check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

SCHEMA = "docx2typed-differential-bundle-prototype-1"
PLAN_SCHEMA = "docx2typed-differential-plan-prototype-1"
REPORT_SCHEMA = "docx2typed-differential-report-prototype-1"
STATUSES = {"pass", "fail", "not-run", "unknown", "invalid"}
REQUIRED_CASE_FIELDS = {
    "id", "kind", "comparison", "status", "result", "diagnostic",
    "semantic_evidence", "effects", "output", "resources",
}
RECOVERY_CASES = [
    "recovery.kill-before-prepare",
    "recovery.kill-after-prepare",
    "recovery.kill-after-external-publish",
    "recovery.kill-after-generation-commit",
    "recovery.enospc",
    "recovery.fsync-failure",
    "recovery.journal-corruption",
    "recovery.writer-race",
]
INTEROP_PHASES = ["open", "render", "save", "reopen", "retention", "repair-observation"]


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object required: {path}")
    return value


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def task_cases(root: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((root / "capabilities" / "tasks").glob("*.json")):
        document = load(path)
        for task in document.get("tasks", []):
            cases.append({
                "id": task["id"],
                "kind": "agent-journey" if path.stem == "agent" else "capability",
                "capability": task.get("capability", "agent.workflow"),
                "source": task["source"],
                "source_sha256": hashlib.sha256((root / task["source"]).read_bytes()).hexdigest(),
                "steps": task.get("steps", []),
                "oracles": task.get("oracles", {}),
            })
    return cases


def make_plan(root: Path, engine_spec: dict[str, Any]) -> dict[str, Any]:
    manifest = load(root / "capabilities" / "manifest.json")
    cases = task_cases(root)
    cases.extend({"id": case, "kind": "recovery"} for case in RECOVERY_CASES)
    consumers = engine_spec.get("interop_consumers", [])
    cases.extend(
        {"id": f"interop.{consumer}.{phase}", "kind": "interop", "consumer": consumer, "phase": phase}
        for consumer in consumers
        for phase in INTEROP_PHASES
    )
    return {
        "schema": PLAN_SCHEMA,
        "engine": engine_spec,
        "contract": {
            "protocol_major": 1,
            "capability_manifest_sha256": digest(manifest),
            "task_set_sha256": digest(cases),
            "canonicalization": "docx2typed-semantic-canonicalization-1",
        },
        "cases": cases,
    }


def validate(bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if bundle.get("schema") != SCHEMA:
        errors.append(f"schema: expected {SCHEMA!r}")
    for field in ("engine", "contract", "cases", "provenance"):
        if field not in bundle:
            errors.append(f"missing bundle field: {field}")
    cases = bundle.get("cases")
    if not isinstance(cases, list):
        return errors + ["cases must be a list"]
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case[{index}] must be an object")
            continue
        missing = REQUIRED_CASE_FIELDS - case.keys()
        if missing:
            errors.append(f"case[{index}] missing: {sorted(missing)}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"case[{index}] invalid id")
        elif case_id in seen:
            errors.append(f"duplicate case: {case_id}")
        else:
            seen.add(case_id)
        if case.get("status") not in STATUSES:
            errors.append(f"case[{index}] invalid status: {case.get('status')!r}")
    return errors


def case_map(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in bundle["cases"]}


def compare(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    for side, bundle in (("oracle", left), ("candidate", right)):
        for error in validate(bundle):
            differences.append({"case": "<bundle>", "field": side, "oracle": None, "candidate": error})
    if differences:
        return report(left, right, differences)

    if left["contract"] != right["contract"]:
        differences.append({"case": "<bundle>", "field": "contract", "oracle": left["contract"], "candidate": right["contract"]})
    left_cases, right_cases = case_map(left), case_map(right)
    if left_cases.keys() != right_cases.keys():
        differences.append({
            "case": "<bundle>", "field": "case-set",
            "oracle": sorted(left_cases), "candidate": sorted(right_cases),
        })
    for case_id in sorted(left_cases.keys() & right_cases.keys()):
        expected, actual = left_cases[case_id], right_cases[case_id]
        for field in ("kind", "comparison", "status", "result", "diagnostic", "semantic_evidence", "effects"):
            if expected[field] != actual[field]:
                differences.append({"case": case_id, "field": field, "oracle": expected[field], "candidate": actual[field]})
        comparison = expected["comparison"]
        if comparison == "package-bytes":
            field = "package_sha256"
        elif comparison == "semantic-signature":
            field = "semantic_signature"
        else:
            differences.append({"case": case_id, "field": "comparison", "oracle": comparison, "candidate": "unsupported comparison"})
            continue
        if expected["output"].get(field) != actual["output"].get(field):
            differences.append({"case": case_id, "field": f"output.{field}", "oracle": expected["output"].get(field), "candidate": actual["output"].get(field)})
        er, ar = expected["resources"], actual["resources"]
        for metric in ("wall_ms", "peak_rss_bytes"):
            oracle_value, candidate_value = er.get(metric), ar.get(metric)
            absolute = er.get(f"{metric}_budget")
            if not isinstance(oracle_value, (int, float)) or not isinstance(candidate_value, (int, float)) or not isinstance(absolute, (int, float)):
                differences.append({"case": case_id, "field": f"resources.{metric}", "oracle": oracle_value, "candidate": candidate_value})
            elif candidate_value > min(oracle_value * 1.10, absolute):
                differences.append({"case": case_id, "field": f"resources.{metric}", "oracle": oracle_value, "candidate": candidate_value})
        oracle_size = expected["output"].get("size_bytes")
        candidate_size = actual["output"].get("size_bytes")
        if isinstance(oracle_size, int) and isinstance(candidate_size, int) and candidate_size > oracle_size + 1_048_576:
            differences.append({"case": case_id, "field": "output.size_bytes", "oracle": oracle_size, "candidate": candidate_size})
    return report(left, right, differences)


def report(left: dict[str, Any], right: dict[str, Any], differences: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "verdict": "pass" if not differences else "fail",
        "oracle": {"engine": left.get("engine"), "bundle_sha256": digest(left)},
        "candidate": {"engine": right.get("engine"), "bundle_sha256": digest(right)},
        "differences": differences,
        "difference_count": len(differences),
    }


def fixture_bundle(engine: str = "python-reference") -> dict[str, Any]:
    case = {
        "id": "calibration.noop",
        "kind": "capability",
        "comparison": "package-bytes",
        "status": "pass",
        "result": {"outcome": "success"},
        "diagnostic": [],
        "semantic_evidence": {"checks": [{"id": "package.identity", "verdict": "pass"}]},
        "effects": {"workdir_generations": 1, "external_outputs": 1},
        "output": {"package_sha256": "a" * 64, "size_bytes": 4096},
        "resources": {
            "wall_ms": 100, "wall_ms_budget": 1000,
            "peak_rss_bytes": 1_000_000, "peak_rss_bytes_budget": 100_000_000,
        },
    }
    return {
        "schema": SCHEMA,
        "engine": {"id": engine, "descriptor_sha256": "b" * 64},
        "contract": {"protocol_major": 1, "task_set_sha256": "c" * 64, "canonicalization": "docx2typed-semantic-canonicalization-1"},
        "cases": [case],
        "provenance": {"runner": "prototype", "raw_samples": True},
    }


def self_check() -> dict[str, Any]:
    left = fixture_bundle()
    right = json.loads(json.dumps(left))
    right["engine"] = {"id": "rust-candidate", "descriptor_sha256": "d" * 64}
    clean = compare(left, right)
    assert clean["verdict"] == "pass", clean
    right["cases"][0]["output"]["package_sha256"] = "e" * 64
    changed = compare(left, right)
    assert changed["verdict"] == "fail", changed
    assert changed["differences"][0]["field"] == "output.package_sha256", changed
    return {"clean_self_compare": clean["verdict"], "seeded_difference": changed["verdict"], "detected_field": changed["differences"][0]["field"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--engine-spec", type=Path, required=True)
    plan_parser.add_argument("--out", type=Path, required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--adapter-command", required=True)
    capture_parser.add_argument("--plan", type=Path, required=True)
    capture_parser.add_argument("--out", type=Path, required=True)
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--oracle", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--out", type=Path, required=True)
    sub.add_parser("self-check")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent

    if args.command == "plan":
        value = make_plan(root, load(args.engine_spec))
        dump(args.out, value)
        print(f"planned {len(value['cases'])} cases -> {args.out}")
        return 0
    if args.command == "capture":
        command = [*shlex.split(args.adapter_command), "run", str(args.plan.resolve()), str(args.out.resolve())]
        run = subprocess.run(command, cwd=root)
        if run.returncode:
            return run.returncode
        errors = validate(load(args.out))
        if errors:
            print(json.dumps({"verdict": "invalid", "errors": errors}, indent=2))
            return 2
        print(f"captured -> {args.out}")
        return 0
    if args.command == "compare":
        value = compare(load(args.oracle), load(args.candidate))
        dump(args.out, value)
        print(json.dumps({"verdict": value["verdict"], "differences": value["difference_count"], "report": str(args.out)}, indent=2))
        return 0 if value["verdict"] == "pass" else 1
    value = self_check()
    print(json.dumps(value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
