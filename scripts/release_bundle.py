"""Immutable signed Reference bundle publisher (issue #54).

Freezes the contract-complete Python engine as the immutable black-box
Reference that authorizes the Rust rewrite.  The release runner executes the
frozen qualification plan twice in independent clean scratch dirs, computes
the deterministic Semantic root of each run (canonical verdict + identity
validation + evidence hashes), requires the two roots to be identical, and
publishes one immutable versioned bundle under ``reference/bundle-<N>/``
(schema ``docx2typed-reference-bundle-1``) that binds:

- source commit/ref and intended release tag,
- engine descriptor + build identity,
- capability manifest, task map, protocol schema bundle,
- fixture identities (corpus manifest, model manifest, fixture hashes),
- resource profiles, office evidence revision,
- canonical Results/Diagnostics/Evidence/signatures/effects from both runs,
- provenance (runner class, host, versions), reproduction commands,
- the signed Semantic root.

Semantic root (schema ``docx2typed-semantic-root-1``): a deterministic
canonical digest over the run's canonical artifacts — the canonical verdict
(plan checks + results, volatile fields stripped per canon
docx2typed-qual-canon-1), the identity validation, and the pinned evidence
hashes.  Two clean runs must produce the same root; any drift fails the
release.

Signing: a detached Ed25519 signature over the bundle's semantic identity
(the canonical JSON of the published manifest) via the ``openssl`` CLI —
toolchain-only, deterministic (RFC 8032), no Python dependencies.  The
public key is committed under ``reference/keys/``; the private key lives in
the local keystore ``~/.docx2typed/keys/`` (or ``$DOCX2TYPED_RELEASE_KEY``).
``--init-dev-key`` provisions a clearly-marked DEV key so the full pipeline
is exercised honestly; release signing requires the operator key (see
``reference/keys/README.md``).  A signature is never fabricated.

Oracle freeze: each bundle carries an immutable freeze record (schema
``docx2typed-oracle-freeze-1``) pinning the plan/capability/schema/evidence
identities and the Semantic root.  The plan's ``oracle-freeze`` check
(scripts/qualify.py) compares the current identities against the newest
recorded freeze and fails closed on drift: the Oracle branch is read-only
for semantic changes; a semantic change requires a classified decision and
a new Oracle major (``--new-oracle-major N --classified "<decision>"``).

Usage:
    python -m scripts.release_bundle                     # run twice, publish bundle-N
    python -m scripts.release_bundle --verify reference/bundle-N
    python -m scripts.release_bundle --reproduce reference/bundle-N
    python -m scripts.release_bundle --init-dev-key
    python -m scripts.release_bundle --new-oracle-major 2 --classified "fix oracle bug X"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

BUNDLE_SCHEMA = "docx2typed-reference-bundle-1"
SEMANTIC_ROOT_SCHEMA = "docx2typed-semantic-root-1"
FREEZE_SCHEMA = "docx2typed-oracle-freeze-1"

DEFAULT_PLAN = REPO_ROOT / "qualification" / "plan.json"
BUNDLE_ROOT = REPO_ROOT / "reference"
KEYS_DIR = BUNDLE_ROOT / "keys"
DEV_PUBKEY = KEYS_DIR / "dev-signing-pub.pem"
OPERATOR_PUBKEY = KEYS_DIR / "release-signing-pub.pem"
KEYSTORE_DIR = Path.home() / ".docx2typed" / "keys"
DEV_PRIVKEY = KEYSTORE_DIR / "dev-signing.key"
RELEASE_PRIVKEY = KEYSTORE_DIR / "release-signing.key"
OPENSSL = "openssl"
TAG_PREFIX = "v"

# The registries/inputs archived verbatim into every bundle so it is
# self-contained; the manifest records each file's identity.
# (office-evidence path is resolved from the plan at publish time.)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReleaseError(ValueError):
    """A release precondition failed; nothing is published."""


# ---------------------------------------------------------------------------
# Source identity (no git invocation: read the plumbing files directly)
# ---------------------------------------------------------------------------


def _resolve_ref_commit(candidates: list[Path], ref: str) -> str | None:
    for base in candidates:
        ref_file = base / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()
    for base in candidates:
        packed = base / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    return None


def resolve_source_commit(root: Path = REPO_ROOT) -> dict[str, Any]:
    """Resolve the checkout's commit id and ref by reading the git plumbing
    files (worktree ``.git`` pointer, HEAD, refs, packed-refs).  Any
    unresolvable step reports ``unknown`` honestly; git is never invoked."""
    try:
        git_file = root / ".git"
        if git_file.is_file():
            line = git_file.read_text(encoding="utf-8").strip()
            gitdir = Path(line.split(":", 1)[1].strip())
            gitdir = gitdir if gitdir.is_absolute() else (root / gitdir)
        else:
            gitdir = root / ".git"
        common: list[Path] = []
        commondir = gitdir / "commondir"
        if commondir.is_file():
            common_line = commondir.read_text(encoding="utf-8").strip()
            common_dir = Path(common_line)
            common_dir = common_dir if common_dir.is_absolute() else (gitdir / common_dir)
            common = [common_dir.resolve()]
        head_ref = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head_ref.startswith("ref: "):
            ref = head_ref[5:].strip()
            commit = _resolve_ref_commit([gitdir, *common], ref)
            return {"commit": commit or "unknown", "ref": ref}
        return {"commit": head_ref, "ref": None}
    except OSError:
        return {"commit": "unknown", "ref": None}


def _bootstrap_build_commit() -> None:
    """Bind the resolved source commit into the engine descriptor before its
    first call (scripts.protocol reads $DOCX2TYPED_BUILD_COMMIT)."""
    if "DOCX2TYPED_BUILD_COMMIT" not in os.environ:
        commit = resolve_source_commit()["commit"]
        if commit != "unknown":
            os.environ["DOCX2TYPED_BUILD_COMMIT"] = commit


_bootstrap_build_commit()

from scripts.protocol import engine_descriptor, file_sha256, schema_bundle, semantic_sha256  # noqa: E402
from scripts.qualify import (  # noqa: E402
    canonical_json,
    canonical_verdict,
    frozen_identities,
    latest_freeze_record,
    plan_semantic_sha256,
    plan_sha256,
    run,
    validate_identities,
    validate_plan,
    validate_report,
)


# ---------------------------------------------------------------------------
# Semantic root
# ---------------------------------------------------------------------------


def evidence_hashes(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan's pinned evidence/registry hashes that participate in the
    Semantic root."""
    ids = plan["identities"]
    return {
        "capability_manifest": ids["capability"]["sha256"],
        "task_map": ids["capability_map"]["sha256"],
        "schema_bundle": ids["contract"]["schema_bundle_sha256"],
        "corpus_manifest": ids["fixture_corpus"]["sha256"],
        "model_manifest": ids["fixture"]["manifest_sha256"],
        "fixture_hashes": ids["fixture"]["fixtures"],
        "resource_profiles": ids["resource_profiles"]["sha256"],
        "office_evidence": ids["office_evidence"]["sha256"],
    }


def compute_semantic_root(
    plan_sha: str,
    checks: list[dict[str, Any]],
    identities: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Deterministic canonical digest over the run's canonical artifacts:
    plan checks + results (canonical verdict), identity validation, and the
    pinned evidence hashes.  Two clean runs must produce the same root."""
    record: dict[str, Any] = {
        "schema": SEMANTIC_ROOT_SCHEMA,
        "plan_sha256": plan_sha,
        "canonical_verdict": canonical_verdict(plan_sha, checks),
        "identities": identities,
        "evidence": evidence,
    }
    digest = _sha256_bytes(canonical_json(record))
    return digest, record


# ---------------------------------------------------------------------------
# One clean release run
# ---------------------------------------------------------------------------


def run_release_run(
    plan: dict[str, Any],
    *,
    root: Path,
    scratch_root: Path,
    index: int,
) -> dict[str, Any]:
    """One full qualification run (with the plan's internal self-comparison)
    in its own independent clean scratch dir; returns the canonical artifacts
    and the run's Semantic root."""
    scratch = scratch_root / f"run-{index}"
    report_dir = scratch / "report"
    report = run(plan, root=root, scratch=scratch / "qualify", report_dir=report_dir)
    validate_report(plan, report)  # coverage audit: no missing/duplicated checks
    plan_sha = plan_sha256(plan)
    identities = validate_identities(plan, root)
    evidence = evidence_hashes(plan)
    digest, record = compute_semantic_root(plan_sha, report["checks"], identities, evidence)
    return {
        "index": index,
        "report": report,
        "plan_sha": plan_sha,
        "canonical_verdict": canonical_verdict(plan_sha, report["checks"]),
        "canonical_verdict_sha256": _sha256_bytes(canonical_json(canonical_verdict(plan_sha, report["checks"]))),
        "identities": identities,
        "evidence": evidence,
        "semantic_root": digest,
        "semantic_root_record": record,
        "report_sha256": _file_sha256(report_dir / "report.json"),
        "report_dir": report_dir,
    }


def _is_blocked_not_run(check: dict[str, Any]) -> bool:
    """A check that failed/not-run because its evidence cannot be produced
    on this host (issue #52 blocking cells: Word macOS/Linux, human repair
    observation).  These are recorded honestly, never fabricated green."""
    if check["result"] not in ("fail", "not-run"):
        return False
    detail = str(check.get("detail", "")).lower()
    return "blocked" in detail or "not-run" in detail or "gate failed closed" in detail


def gate_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"checks": {}, "pass": [], "fail": [], "skip": [], "not-run": [], "error": []}
    blocked: list[dict[str, Any]] = []
    for check in checks:
        summary["checks"][check["id"]] = {"result": check["result"], "detail": check.get("detail", "")}
        summary[check["result"] if check["result"] != "not-run" else "not-run"].append(check["id"])
        if _is_blocked_not_run(check):
            blocked.append({"id": check["id"], "result": check["result"], "detail": check.get("detail", "")})
    summary["blocked_not_run"] = blocked
    real_failures = [check["id"] for check in checks if check["result"] in ("fail", "not-run", "error") and not _is_blocked_not_run(check)]
    # release_ready means TRUE full green: no real failures AND no
    # host-blocked-not-run cells.  A bundle may still be published with
    # blocked-not-run cells (issue #52 records the honest verdict), but it
    # must never claim full green.
    summary["release_ready"] = not real_failures and not blocked
    summary["release_ready_reasons"] = [
        *(f"{check_id}: not green and not host-blocked" for check_id in real_failures),
        *(f"{b['id']}: blocked-not-run on this host ({b['result']}): {b['detail'][:160]}" for b in blocked),
    ]
    return summary


# ---------------------------------------------------------------------------
# Bundle publishing
# ---------------------------------------------------------------------------


def next_bundle_dir(root: Path = BUNDLE_ROOT) -> Path:
    existing: list[int] = []
    if root.is_dir():
        for path in root.glob("bundle-*"):
            if path.is_dir():
                match = __import__("re").fullmatch(r"bundle-(\d+)", path.name)
                if match:
                    existing.append(int(match.group(1)))
    number = max(existing, default=0) + 1
    bundle_dir = root / f"bundle-{number}"
    if bundle_dir.exists():
        raise ReleaseError(f"bundle directory already exists: {bundle_dir} (immutable; refusing to overwrite)")
    return bundle_dir


def resolve_oracle_major(
    options: argparse.Namespace, prior: dict[str, Any] | None, plan: dict[str, Any]
) -> tuple[int, str]:
    """The Oracle major for this release plus its classified decision."""
    if options.oracle_major is not None:
        if prior is None:
            return options.oracle_major, options.classified or "initial Oracle freeze"
        if options.oracle_major <= prior.get("oracle_major", 0):
            raise ReleaseError("--new-oracle-major must exceed the recorded Oracle major")
        if not options.classified:
            raise ReleaseError("a new Oracle major requires --classified <decision>")
        current = frozen_identities(plan)
        recorded = prior.get("frozen", {}).get("identities", {})
        drifted = sorted(key for key in current if current[key] != recorded.get(key))
        if not drifted:
            raise ReleaseError("no semantic drift to classify: identities are unchanged since the recorded freeze")
        return options.oracle_major, options.classified
    if prior is None:
        return 1, "initial Oracle freeze"
    return prior.get("oracle_major", 1), None


def build_freeze_record(
    *,
    oracle_major: int,
    decision: str,
    plan: dict[str, Any],
    plan_sha: str,
    semantic_root: str,
    bundle_dir: Path,
) -> dict[str, Any]:
    try:
        bundle_rel = str(bundle_dir.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # bundle published outside the repo (tests)
        bundle_rel = str(bundle_dir).replace("\\", "/")
    record: dict[str, Any] = {
        "schema": FREEZE_SCHEMA,
        "oracle_major": oracle_major,
        "bundle": bundle_rel,
        "generated": _now_iso(),
        "frozen": {
            "plan_sha256": plan_sha,
            "plan_semantic_sha256": plan_semantic_sha256(plan),
            "semantic_root_sha256": semantic_root,
            "identities": frozen_identities(plan),
        },
        "policy": (
            "The Oracle branch is read-only for semantic changes after freeze. "
            "Any drift of the plan/capability/schema/evidence identities from "
            "this record fails the oracle-freeze qualification check and blocks "
            "release until a classified decision records a new Oracle major."
        ),
        "history": [],
    }
    prior = latest_freeze_record(REPO_ROOT)
    if prior is not None:
        record["history"] = [dict(entry) for entry in prior[0].get("history", [])]
    record["history"].append(
        {
            "oracle_major": oracle_major,
            "decision": decision,
            "bundle": bundle_rel,
            "plan_sha256": plan_sha,
            "semantic_root_sha256": semantic_root,
        }
    )
    return record


def _input_copies(plan: dict[str, Any]) -> dict[str, str]:
    """Logical input name -> repo-relative path, resolved from the plan."""
    ids = plan["identities"]
    return {
        "plan.json": "qualification/plan.json",
        "capability-manifest.json": ids["capability"]["path"],
        "task-map.json": ids["capability_map"]["path"],
        "schema-bundle.json": "scripts/protocol_schema_bundle.json",
        "corpus-manifest.json": ids["fixture_corpus"]["path"],
        "model-manifest.json": ids["fixture"]["manifest_path"],
        "resource-profiles.json": ids["resource_profiles"]["path"],
        "office-evidence.json": ids["office_evidence"]["path"],
        "plain.docx": "corpus/release/plain.docx",
    }


def publish_bundle(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    options: argparse.Namespace,
) -> dict[str, Any]:
    validate_plan(plan)
    plan_sha = plan_sha256(plan)
    roots = [run["semantic_root"] for run in runs]
    if len(set(roots)) != 1:
        raise ReleaseError(
            "Semantic roots differ across the two clean runs: "
            + " vs ".join(f"run-{run['index']}={run['semantic_root']}" for run in runs)
        )
    for run_data in runs:
        self_check = next((c for c in run_data["report"]["checks"] if c["id"] == "self-comparison"), None)
        if self_check is None or self_check["result"] != "pass":
            raise ReleaseError(
                f"run-{run_data['index']}: internal self-comparison did not pass "
                f"({self_check and self_check['result']}); canonical verdicts differ within the run"
            )
    summary = gate_summary(runs[0]["report"]["checks"])
    real_failures = [reason for reason in summary["release_ready_reasons"] if "blocked-not-run" not in reason]
    if real_failures:
        raise ReleaseError(
            "release blocked by non-host-blocked failures: " + "; ".join(real_failures)
        )

    prior = latest_freeze_record(REPO_ROOT)
    prior_record = prior[0] if prior is not None else None
    oracle_major, decision = resolve_oracle_major(options, prior_record, plan)

    bundle_dir = next_bundle_dir()
    (bundle_dir / "runs").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "signatures").mkdir(parents=True, exist_ok=True)

    # Archive the frozen registries so the bundle is self-contained.
    inputs: dict[str, dict[str, str]] = {}
    for name, relative in _input_copies(plan).items():
        source = REPO_ROOT / relative
        if not source.is_file():
            raise ReleaseError(f"input {relative} missing; cannot archive the bundle")
        shutil.copy2(source, bundle_dir / "inputs" / name)
        inputs[name] = {"path": relative, "sha256": _file_sha256(bundle_dir / "inputs" / name)}

    # Per-run canonical artifacts.
    for run_data in runs:
        run_dir = bundle_dir / f"runs/run-{run_data['index']}"
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(run_data["report_dir"] / "report.json", run_dir / "report.json")
        shutil.copy2(run_data["report_dir"] / "verdict.json", run_dir / "verdict.json")
        (run_dir / "canonical-verdict.json").write_text(
            json.dumps(run_data["canonical_verdict"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "identities.json").write_text(
            json.dumps(run_data["identities"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "evidence-hashes.json").write_text(
            json.dumps(run_data["evidence"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "semantic-root.json").write_text(
            json.dumps(
                {"sha256": run_data["semantic_root"], "record": run_data["semantic_root_record"]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    root_digest, root_record = runs[0]["semantic_root"], runs[0]["semantic_root_record"]
    freeze = build_freeze_record(
        oracle_major=oracle_major,
        decision=decision,
        plan=plan,
        plan_sha=plan_sha,
        semantic_root=root_digest,
        bundle_dir=bundle_dir,
    )
    (bundle_dir / "freeze.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    source = resolve_source_commit(REPO_ROOT)
    engine = engine_descriptor()
    ids = plan["identities"]
    try:
        bundle_rel = str(bundle_dir.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # bundle published outside the repo (tests)
        bundle_rel = str(bundle_dir).replace("\\", "/")
    manifest: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "oracle_major": oracle_major,
        "bundle": bundle_rel,
        "generated": _now_iso(),
        "source": source,
        "tag": options.tag or f"{TAG_PREFIX}{engine.get('version', '0.0.0')}-oracle-{oracle_major}",
        "engine": engine,
        "runner": {
            "class": "scripts.release_bundle",
            "schema": BUNDLE_SCHEMA,
            "host": platform.node(),
            "platform": sysconfig.get_platform(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
        },
        "plan": {"path": "qualification/plan.json", "sha256": plan_sha},
        "registries": {
            "capability_manifest": {"path": ids["capability"]["path"], "sha256": ids["capability"]["sha256"]},
            "task_map": {"path": ids["capability_map"]["path"], "sha256": ids["capability_map"]["sha256"]},
            "schema_bundle": {
                "path": "scripts/protocol_schema_bundle.json",
                "sha256": ids["contract"]["schema_bundle_sha256"],
            },
            "corpus_manifest": {"path": ids["fixture_corpus"]["path"], "sha256": ids["fixture_corpus"]["sha256"]},
            "model_manifest": {"path": ids["fixture"]["manifest_path"], "sha256": ids["fixture"]["manifest_sha256"]},
            "resource_profiles": {
                "path": ids["resource_profiles"]["path"],
                "sha256": ids["resource_profiles"]["sha256"],
            },
            "office_evidence": {
                "revision": ids["office_evidence"]["path"].split("/")[-2],
                "path": ids["office_evidence"]["path"],
                "sha256": ids["office_evidence"]["sha256"],
            },
        },
        "fixture_identities": {"fixtures": ids["fixture"]["fixtures"]},
        "runs": [
            {
                "id": f"run-{run_data['index']}",
                "dir": f"runs/run-{run_data['index']}",
                "semantic_root": run_data["semantic_root"],
                "canonical_verdict_sha256": run_data["canonical_verdict_sha256"],
                "report_sha256": run_data["report_sha256"],
                "verdict": run_data["report"]["verdict"],
            }
            for run_data in runs
        ],
        "semantic_root": {"sha256": root_digest, "record": root_record},
        "gate_summary": summary,
        "reproduction": {
            "command": f"python -m scripts.release_bundle --reproduce {bundle_rel}",
            "inputs": "committed corpus (corpus/) and pinned plan (qualification/plan.json); archived copies in inputs/",
            "expect": "a fresh clean run reproduces the recorded Semantic root",
        },
        "inputs": inputs,
        "freeze": {"path": "freeze.json", "schema": FREEZE_SCHEMA},
    }

    key_path, key_role, key_id = load_signing_key()
    manifest["signing"] = {
        "algorithm": "ed25519",
        "key_role": key_role,
        "key_id": key_id,
        "signed": "canonical JSON of this manifest excluding the signature field (bundle semantic identity)",
        "message_sha256": None,
        "signature": None,
        "verification": "openssl pkeyutl -verify -pubin -inkey reference/keys/<pub>.pem -rawin -in message.bin -sigfile signature.sig",
    }
    # Self-consistent detached signature: the payload is the canonical JSON
    # of the published manifest minus the signature field, so verification
    # recomputes the identical bytes from the on-disk manifest.
    payload = _signed_payload(manifest)
    manifest["signing"]["signature"] = sign_message(payload, key_path)
    manifest["signing"]["message_sha256"] = _sha256_bytes(payload)

    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (bundle_dir / "semantic-root.json").write_text(
        json.dumps({"sha256": root_digest, "record": root_record}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "signatures" / "semantic-identity.sig.json").write_text(
        json.dumps(
            {
                "schema": "docx2typed-detached-signature-1",
                "algorithm": "ed25519",
                "key_role": key_role,
                "key_id": key_id,
                "message": "canonical JSON of manifest.json",
                "message_sha256": manifest["signing"]["message_sha256"],
                "signature_base64": manifest["signing"]["signature"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Immediate self-check: the published bundle must verify.
    verification = verify_bundle(bundle_dir)
    if not verification["ok"]:
        raise ReleaseError(f"published bundle fails its own verification: {verification['errors']}")

    return {
        "bundle_dir": bundle_dir,
        "semantic_root": root_digest,
        "runs": [run_data["semantic_root"] for run_data in runs],
        "gate_summary": summary,
        "signing": {"key_role": key_role, "key_id": key_id},
        "oracle_major": oracle_major,
        "decision": decision,
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# Signing (Ed25519 via the openssl CLI, toolchain-only)
# ---------------------------------------------------------------------------


def key_public_pem(private_key: Path) -> bytes:
    result = subprocess.run(
        [OPENSSL, "pkey", "-in", str(private_key), "-pubout"],
        check=True,
        capture_output=True,
    )
    return result.stdout


def sign_message(message: bytes, private_key: Path) -> str:
    """Detached Ed25519 signature (base64) over ``message``.  Ed25519 is
    deterministic (RFC 8032): the same message and key always produce the
    same signature, so re-verification is exact."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        msg = tmp_dir / "message.bin"
        sig = tmp_dir / "signature.sig"
        msg.write_bytes(message)
        subprocess.run(
            [OPENSSL, "pkeyutl", "-sign", "-inkey", str(private_key), "-rawin", "-in", str(msg), "-out", str(sig)],
            check=True,
            capture_output=True,
        )
        return base64.b64encode(sig.read_bytes()).decode("ascii")


def verify_signature(message: bytes, signature_b64: str, public_key: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        msg = tmp_dir / "message.bin"
        sig = tmp_dir / "signature.sig"
        msg.write_bytes(message)
        sig.write_bytes(base64.b64decode(signature_b64))
        result = subprocess.run(
            [OPENSSL, "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key), "-rawin", "-in", str(msg), "-sigfile", str(sig)],
            capture_output=True,
        )
        return result.returncode == 0 and b"Verified" in result.stdout


def load_signing_key() -> tuple[Path, str, str]:
    """The private key to sign with plus its role.  The operator key (env or
    keystore) wins; the clearly-marked dev key is the fallback.  The role is
    decided by which committed public key the key material matches; an
    unregistered key is refused so a signature can never be unverifiable."""
    candidates: list[tuple[Path, str]] = []
    env_key = os.environ.get("DOCX2TYPED_RELEASE_KEY")
    if env_key:
        candidates.append((Path(env_key), "env"))
    if RELEASE_PRIVKEY.is_file():
        candidates.append((RELEASE_PRIVKEY, "keystore-release"))
    if DEV_PRIVKEY.is_file():
        candidates.append((DEV_PRIVKEY, "keystore-dev"))
    if not candidates:
        raise ReleaseError(
            "no signing key available: set DOCX2TYPED_RELEASE_KEY, install the operator key in "
            f"{RELEASE_PRIVKEY}, or provision the dev key with --init-dev-key"
        )
    key_path = candidates[0][0]
    public_pem = key_public_pem(key_path)
    key_id = _sha256_bytes(public_pem)
    if DEV_PUBKEY.is_file() and _sha256_bytes(DEV_PUBKEY.read_bytes()) == key_id:
        role = "dev"
    elif OPERATOR_PUBKEY.is_file() and _sha256_bytes(OPERATOR_PUBKEY.read_bytes()) == key_id:
        role = "operator"
    else:
        raise ReleaseError(
            f"signing key {key_path} is not registered: its public key is not committed under reference/keys/ "
            "(commit it or use the dev key); a signature must always be verifiable"
        )
    return key_path, role, key_id


def init_dev_key() -> dict[str, Any]:
    """Provision the clearly-marked DEV key pair: private key in the local
    keystore (never committed), public key committed under reference/keys/."""
    KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if not DEV_PRIVKEY.is_file():
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", str(DEV_PRIVKEY)], check=True, capture_output=True)
    try:
        os.chmod(DEV_PRIVKEY, 0o600)
    except OSError:  # Windows has no chmod semantics for this
        pass
    public_pem = key_public_pem(DEV_PRIVKEY)
    DEV_PUBKEY.write_bytes(public_pem)
    return {
        "private_key": str(DEV_PRIVKEY),
        "public_key": str(DEV_PUBKEY),
        "key_id": _sha256_bytes(public_pem),
        "role": "dev",
        "note": "DEV key: exercises the full signing pipeline honestly. Release signing requires the operator key (reference/keys/README.md).",
    }


# ---------------------------------------------------------------------------
# Verification / reproduction
# ---------------------------------------------------------------------------


def _committed_public_key(key_id: str) -> Path | None:
    for path in (DEV_PUBKEY, OPERATOR_PUBKEY):
        if path.is_file() and _sha256_bytes(path.read_bytes()) == key_id:
            return path
    return None


def _signed_payload(manifest: dict[str, Any]) -> bytes:
    """The bundle's semantic identity: canonical JSON of the manifest with
    the signature-derived fields (``signature``, ``message_sha256``)
    excluded.  The payload is identical at publish time and at verification
    time, so the detached signature is recomputable from the published
    manifest."""
    signing = dict(manifest.get("signing", {}))
    signing.pop("signature", None)
    signing.pop("message_sha256", None)
    payload = dict(manifest)
    payload["signing"] = signing
    return canonical_json(payload)


def verify_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Independent audit of one published bundle: schema, archived-input
    hashes, detached signature, run-root equality, per-run artifact hashes,
    and freeze-record consistency."""
    errors: list[str] = []
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "errors": ["manifest.json missing"], "key_id": None, "role": None}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != BUNDLE_SCHEMA:
        errors.append(f"manifest schema {manifest.get('schema')!r} != {BUNDLE_SCHEMA}")
    for name, info in manifest.get("inputs", {}).items():
        path = bundle_dir / "inputs" / name
        if not path.is_file():
            errors.append(f"input {name} missing from bundle")
        elif _file_sha256(path) != info.get("sha256"):
            errors.append(f"input {name} hash drift")
    signing = manifest.get("signing", {})
    public_key = _committed_public_key(signing.get("key_id", ""))
    payload = _signed_payload(manifest)
    if _sha256_bytes(payload) != signing.get("message_sha256"):
        errors.append("signing.message_sha256 does not match the signed payload")
    if public_key is None:
        errors.append(f"no committed public key matches key_id {signing.get('key_id')}")
    elif not verify_signature(payload, signing.get("signature", ""), public_key):
        errors.append("detached signature does not verify over manifest.json")
    roots = [run.get("semantic_root") for run in manifest.get("runs", [])]
    if len(roots) != 2 or len(set(roots)) != 1:
        errors.append(f"runs do not agree on the semantic root: {roots}")
    elif roots[0] != manifest.get("semantic_root", {}).get("sha256"):
        errors.append("recorded semantic root does not match the runs")
    for run_data in manifest.get("runs", []):
        run_dir = bundle_dir / run_data.get("dir", "")
        if not (run_dir / "report.json").is_file():
            errors.append(f"{run_data.get('dir')}/report.json missing")
        elif _file_sha256(run_dir / "report.json") != run_data.get("report_sha256"):
            errors.append(f"{run_data.get('dir')}/report.json hash drift")
        if (run_dir / "canonical-verdict.json").is_file():
            cv = json.loads((run_dir / "canonical-verdict.json").read_text(encoding="utf-8"))
            if _sha256_bytes(canonical_json(cv)) != run_data.get("canonical_verdict_sha256"):
                errors.append(f"{run_data.get('dir')}/canonical-verdict.json hash drift")
        if (run_dir / "semantic-root.json").is_file():
            sr = json.loads((run_dir / "semantic-root.json").read_text(encoding="utf-8"))
            if sr.get("sha256") != run_data.get("semantic_root"):
                errors.append(f"{run_data.get('dir')}/semantic-root.json does not record the run root")
    freeze_path = bundle_dir / "freeze.json"
    if freeze_path.is_file():
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        if freeze.get("schema") != FREEZE_SCHEMA:
            errors.append("freeze.json schema mismatch")
        if freeze.get("frozen", {}).get("semantic_root_sha256") != manifest.get("semantic_root", {}).get("sha256"):
            errors.append("freeze record does not pin the manifest semantic root")
    else:
        errors.append("freeze.json missing from bundle")
    return {
        "ok": not errors,
        "errors": errors,
        "key_id": signing.get("key_id"),
        "role": signing.get("key_role"),
        "semantic_root": manifest.get("semantic_root", {}).get("sha256"),
    }


def reproduce(bundle_dir: Path, plan_path: Path, work: Path) -> dict[str, Any]:
    """Rebuild from the archived inputs (committed corpus + pinned plan) and
    require the fresh run's Semantic root to equal the recorded root."""
    verification = verify_bundle(bundle_dir)
    if not verification["ok"]:
        return {"ok": False, "detail": "bundle verification failed: " + "; ".join(verification["errors"])}
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    recorded_root = manifest["semantic_root"]["sha256"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    validate_plan(plan)
    scratch = work / "reproduce"
    report = run(plan, root=REPO_ROOT, scratch=scratch / "qualify", report_dir=scratch / "report")
    identities = validate_identities(plan, REPO_ROOT)
    digest, record = compute_semantic_root(
        plan_sha256(plan), report["checks"], identities, evidence_hashes(plan)
    )
    same = digest == recorded_root
    freeze_check = next((c for c in report["checks"] if c["id"] == "oracle-freeze"), None)
    return {
        "ok": same,
        "recorded_root": recorded_root,
        "reproduced_root": digest,
        "semantic_root_identical": same,
        "oracle_freeze_check": freeze_check["result"] if freeze_check else None,
        "oracle_freeze_detail": freeze_check.get("detail", "") if freeze_check else "",
        "detail": (
            "reproduced Semantic root is identical to the recorded root"
            if same
            else f"reproduced root {digest} != recorded root {recorded_root}"
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=str(DEFAULT_PLAN), help="frozen plan path")
    parser.add_argument("--work", default=None, help="scratch root (default: temp)")
    parser.add_argument("--tag", default=None, help="release tag recorded in the bundle")
    parser.add_argument("--new-oracle-major", type=int, default=None, dest="oracle_major", help="classify a semantic change as a new Oracle major")
    parser.add_argument("--classified", default=None, help="classified decision for a new Oracle major")
    parser.add_argument("--verify", metavar="BUNDLE", help="audit a published bundle")
    parser.add_argument("--reproduce", metavar="BUNDLE", help="re-run the plan and require the recorded Semantic root")
    parser.add_argument("--init-dev-key", action="store_true", help="provision the dev signing key pair")
    args = parser.parse_args(argv)

    if args.init_dev_key:
        info = init_dev_key()
        print(f"dev key ready: {info['private_key']}")
        print(f"public key committed: {info['public_key']} (key_id {info['key_id']})")
        print(f"note: {info['note']}")
        return 0

    if args.verify or args.reproduce:
        bundle_dir = Path(args.verify or args.reproduce)
        if not (bundle_dir / "manifest.json").is_file():
            print(f"error: {bundle_dir} is not a published bundle (no manifest.json)")
            return 2
        if args.verify:
            verification = verify_bundle(bundle_dir)
            print(f"bundle: {bundle_dir}")
            print(f"signature: {'valid' if verification['ok'] else 'INVALID'} "
                  f"(role={verification.get('role')} key_id={verification.get('key_id')})")
            print(f"semantic root: {verification.get('semantic_root')}")
            if not verification["ok"]:
                for error in verification["errors"]:
                    print(f"  FAIL {error}")
                return 1
            print("verification: PASS (schema, inputs, signature, run roots, artifacts, freeze record)")
            return 0
        work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="docx2typed-reproduce-"))
        work.mkdir(parents=True, exist_ok=True)
        result = reproduce(bundle_dir, Path(args.plan), work)
        print(f"reproduce: {result['detail']}")
        print(f"  recorded root:    {result['recorded_root']}")
        print(f"  reproduced root:  {result['reproduced_root']}")
        print(f"  oracle-freeze check in fresh run: {result.get('oracle_freeze_check')} — {result.get('oracle_freeze_detail')}")
        return 0 if result["ok"] else 1

    plan_path = Path(args.plan)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        validate_plan(plan)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"plan error: {exc}")
        return 2
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="docx2typed-release-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        # Two independent clean release runs.
        run_1 = run_release_run(plan, root=REPO_ROOT, scratch_root=work, index=1)
        run_2 = run_release_run(plan, root=REPO_ROOT, scratch_root=work, index=2)
        result = publish_bundle(plan, [run_1, run_2], args)
    except ReleaseError as exc:
        print(f"release error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - runner failures are fatal
        print(f"release error: {type(exc).__name__}: {exc}")
        return 1
    summary = result["gate_summary"]
    print(f"bundle published: {result['bundle_dir']}")
    print(f"semantic root (both runs identical): {result['semantic_root']}")
    print(f"oracle major: {result['oracle_major']} — {result['decision']}")
    print(f"signing: role={result['signing']['key_role']} key_id={result['signing']['key_id']}")
    print(f"signature self-check: {'PASS' if result['verification']['ok'] else 'FAIL'}")
    print(f"release_ready: {summary['release_ready']}")
    if summary["release_ready_reasons"]:
        for reason in summary["release_ready_reasons"]:
            print(f"  blocked: {reason}")
    for blocked in summary["blocked_not_run"]:
        print(f"  blocked-not-run: {blocked['id']} ({blocked['result']}): {blocked['detail'][:160]}")
    print(f"per-check results: {json.dumps(summary['checks'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
