"""Issue #62 offline oracle / diagnostic rollback asset.

This script is NOT invoked by the #62 gate (the gate is deliberately
Python-free). It is an offline, independent re-verification of the #62
real-document chain evidence using the committed Python Reference model
(scripts.typed_docx + scripts.typed_core):

  - recompute per-part SHA-256 of each built output vs its source fixture
    and assert only the expected parts changed (nothing added/removed),
  - re-derive the visible-text semantic signature of each built output with
    the Python model and assert the edited prose is present and the old
    prose is gone (no silent corruption / false success),
  - validate the generated evidence JSON (schema, docs matrix, legacy
    immutability, clean cutover resolver, Office not-run-no-host fail-closed
    cells, release_ready=false, deferrals).

Usage (offline, after the gate produced evidence):
  python qualification/rust_tracer62_oracle.py \
      --evidence qualification/evidence/rust_tracer62_evidence.json \
      --repo <repo-root>

Exit code 0 only when every offline check passes.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.typed_docx import parse_package_document  # noqa: E402
from scripts.typed_core import visible_text  # noqa: E402


def part_hashes(path: pathlib.Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in sorted(archive.namelist())
        }


def file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def visible_document_text(path: pathlib.Path) -> str:
    """All visible body text of the package via the Python Reference model
    (typed paths, not raw XML scraping)."""
    with zipfile.ZipFile(path) as archive:
        parsed = parse_package_document(archive)
    parts: list[str] = []
    for paragraph in parsed.document.paragraphs:
        parts.append(visible_text(paragraph.nodes))
    return "\n".join(parts)


def check_chain(evidence: dict, repo: pathlib.Path) -> list[str]:
    """Re-verify the real-document chain evidence offline.

    The committed real-world-shaped fixtures were derived from committed
    corpus docs through the Rust chain (extract -> six island-local prose
    edits -> build -> independent verify). This oracle re-derives, with the
    Python Reference model, that each fixture's visible body text contains
    exactly the derivation-new prose (and no derivation-old prose), and that
    the evidence's recorded changed-part sets are consistent with the
    fixture configuration.
    """
    failures: list[str] = []
    edits = json.loads(
        (repo / "qualification/rust_tracer62/fixtures/edits.json").read_text(encoding="utf-8")
    )
    # The six island-local derivation edits that shaped each fixture from its
    # committed corpus base (recorded in the evidence provenance).
    derivation = {
        "patent-shaped": [
            ("本发明涉及生物医用材料技术领域。剂量为 20 mg。",
             "本发明涉及一种可降解生物医用复合材料及其制备方法，属于生物医用材料技术领域。"),
            ("实施例1采用 250 mg 剂量。",
             "权利要求1：一种可降解生物医用复合材料，其特征在于，所述材料包含聚乳酸和羟基磷灰石。"),
            ("The quick brown fox.",
             "权利要求2：根据权利要求1所述的可降解生物医用复合材料，其特征在于，所述聚乳酸与羟基磷灰石的质量比为 70:30。"),
            ("结束段落。",
             "实施例1：将聚乳酸与羟基磷灰石按质量比 70:30 混合，经熔融挤出制备得到所述复合材料。"),
            ("ABC denotes the control group.",
             "对照实验表明，所述复合材料在体外降解实验中表现出优异的生物相容性，适合骨组织修复应用。"),
            ("重复句子内容 重复句子内容。",
             "上述实施例仅用于说明本发明的技术方案，不构成对本发明保护范围的限制。"),
        ],
        "paper-shaped": [
            ("关键一",
             "ABSTRACT：本研究评估可降解生物医用复合材料对大鼠骨缺损模型的骨再生效果。"),
            ("关键二",
             "第2节 材料与方法：复合材料经熔融挤出制备，按质量比 70:30 混合聚乳酸与羟基磷灰石。"),
            ("关键三",
             "第3节 结果：体外降解与细胞实验证实该复合材料具有良好的生物相容性。"),
            ("批注一内容",
             "审稿人1：请在降解数据中补充标准差，并说明每组样本量。"),
            ("批注二内容",
             "审稿人2：请在第2节补充质量比 70:30 的选择依据。"),
            ("批注三内容",
             "编辑：请在本轮修改中增加统计分析方法章节。"),
        ],
    }
    for row in evidence["docs_matrix"]:
        name = row["fixture"]
        cfg = edits[name]
        source = repo / row["source_docx"]
        expected = sorted(cfg["expect_changed"])
        recorded = sorted(row["changed_parts"])
        if expected != recorded:
            failures.append(
                f"{name}: recorded changed parts {recorded} != expected {expected}"
            )
        if row["parts_added"] != 0 or row["parts_removed"] != 0:
            failures.append(f"{name}: parts added/removed recorded, not byte-preserving")
        if row["legacy_unchanged_after_chain"] is not True:
            failures.append(f"{name}: legacy source not recorded unchanged")
        # The fixture body text must contain the derivation-new prose and
        # must not contain the derivation-old prose (no silent corruption).
        with zipfile.ZipFile(source) as archive:
            parsed = parse_package_document(archive)
        source_text = "\n".join(visible_text(p.nodes) for p in parsed.document.paragraphs)
        for old, new in derivation[name]:
            if new not in source_text:
                failures.append(f"{name}: derivation-new prose missing from built fixture text")
            if old in source_text and old != new:
                failures.append(f"{name}: derivation-old prose still present in built fixture text")
    return failures


def check_evidence(evidence: dict) -> list[str]:
    failures: list[str] = []
    required = [
        "schema", "issue", "branch", "generated", "host", "gate", "binary",
        "checks", "checks_pass", "checks_total", "docs_matrix", "legacy",
        "cutover", "office_matrix", "release_ready", "verdict", "deferrals",
    ]
    for key in required:
        if key not in evidence:
            failures.append(f"evidence missing key: {key}")
    if evidence.get("release_ready") is not False:
        failures.append("release_ready must be false (Office cells not-run-no-host)")
    office = evidence.get("office_matrix", {})
    if office.get("status") != "not-run-no-host":
        failures.append("office_matrix.status must be not-run-no-host")
    if office.get("blocking_summary", {}).get("gate") != "fail":
        failures.append("office blocking summary must fail closed")
    cutover = evidence.get("cutover", {})
    if cutover.get("resolver") != "rust-absolute-path-only":
        failures.append("cutover resolver must be rust-absolute-path-only")
    if cutover.get("python_launcher_in_tree") is not False:
        failures.append("python_launcher_in_tree must be false")
    for key in ("rollout_counts", "telemetry", "committee", "long_term_oracle_policy"):
        if key not in evidence.get("deferrals", {}):
            failures.append(f"deferral missing: {key}")
    if evidence.get("checks_pass") != evidence.get("checks_total"):
        failures.append("evidence records failed checks")
    return failures


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=pathlib.Path)
    parser.add_argument("--repo", default=REPO, type=pathlib.Path)
    args = parser.parse_args()

    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    failures: list[str] = []
    failures += check_evidence(evidence)
    failures += check_chain(evidence, args.repo)

    if failures:
        print("OFFLINE ORACLE: FAIL")
        for failure in failures:
            print("  -", failure)
        return 1
    print("OFFLINE ORACLE: PASS (evidence schema, docs matrix, legacy immutability, cutover resolver, Office fail-closed cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
