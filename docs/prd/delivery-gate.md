# Delivery gate: a trusted-delivery protocol

Status: **design (P0-B-Gate)**. Depends on P0-B-Evidence
(`docx2typed-delivery-1`, merged in the build evidence payload).

Background and measurements: [`docs/research/real-world-patent-usage-findings.md`](../research/real-world-patent-usage-findings.md) §11.

## Problem

`build` already verifies before publishing (ADR 0022), and P0-B-Evidence now
records *what* was verified *for which artifact bytes and version*. What is
still missing is the **refusal face**: nothing consults that record at delivery
time, so an artifact whose bytes changed after verification is still
indistinguishable — to a caller or a user — from a verified one.

Measured on the patent corpus: of 49 verify-evidence files, 2 had artifacts
whose hash no longer matched (one edited 1 h after verify, one 4 days after),
and 6 pointed at missing targets. A one-shot signature is not enough.

## The three frozen decisions

These must be fixed before implementation; the Gate must not become a second
system overlapping workspace identity, Version, or the ADRs.

### 1. Evidence format (frozen)

`docx2typed-delivery-1`, already emitted by both build entry points:

```json
{
  "schema": "docx2typed-delivery-1",
  "artifact_sha256": "…",
  "verification": { "methods": ["package_guard", "verify_workdir"],
                    "verdict": "pass", "policy_version": "1" },
  "source": { "family_id": "f_…", "version_id": "V23", "tree_sha256": "…" }
}
```

Rules already enforced by `protocol.delivery_record()`:

- `methods` names only checks that **actually ran**; never padded from a shape.
- the binding is to **content + version**, never a path.
- `verdict` is one of `pass` / `fail`.

The Gate reads this record; it does not invent a second one.

### 2. Trust boundary (frozen)

Two capabilities, explicitly separated:

| Level | Question | Who can answer it |
|---|---|---|
| **Consistency** | have these bytes changed since verification? | the engine, today |
| **Origin proof** | was this record produced by the trusted engine, not forged? | needs a protected publisher / isolation / signing |

**P0-B-Gate implements consistency only.** It must be documented — in the tool
description and the refusal message — as *not* forgery-proof. An actor able to
rewrite both the artifact and its sidecar can forge a `pass`; that is out of
scope until a protected publish boundary exists.

### 3. Freshness rule (frozen)

Freshness is **byte-derived**, never workspace-derived:

- an artifact whose bytes are unchanged is `verified-current`, **even if HEAD
  has advanced** (a later workspace edit does not un-verify an earlier export);
- an artifact whose bytes changed is `modified-after-verify`, regardless of
  what the workspace says;
- fresh evaluation uses a **single hash comparison** against
  `artifact_sha256` — no time-based expiry.

This is the invariant `protocol.delivery_status()` already encodes.

## The two faces

### A. Query face — `delivery_status`

A read-only tool that answers, for one artifact path (or the session's last
build output), which state it is in:

| State | Meaning | Trusted? |
|---|---|---|
| `verified-current` | bytes match the delivery record; verdict `pass` | **yes** |
| `modified-after-verify` | a record exists but the bytes differ | no |
| `unregistered` | no readable record (or non-`pass` verdict) | no |
| `missing` | the artifact path does not exist | no |
| `external-imported` | known external origin, not yet re-verified | no |

This mirrors `protocol.delivery_status(record, artifact_sha256=…)` exactly; the
tool adds filesystem resolution and refuses nothing (it reports).

### B. Refusal face — the gate

A **trusted-delivery** assertion any caller can require before treating a file
as deliverable. The natural place is `verify_output`: it already binds the
output hash, the snapshot, and the edit state, and it already fails closed on
mismatch. The Gate adds: after a successful verify, the emitted delivery record
makes the artifact `verified-current`, and a *subsequent* delivery query on
changed bytes returns `modified-after-verify` and is refused.

Deliberately **not** proposed: a filesystem-level block on writing DOCX. As
§11.10 states, with a Python-capable actor and no isolation, "all user-delivered
files are controlled" is **not a property this architecture can guarantee**.
The gate guarantees the narrower, testable property below.

## Acceptance

> Artifacts delivered **through the trusted publish path** are 100% covered by
> a content-bound verification record; artifacts that are unverified or whose
> record is stale are 100% refused by the gate.

Not accepted as a criterion: "every DOCX on disk has evidence".

Required scenarios (real samples), per §11.10:

1. build → modify the artifact → query says `modified-after-verify`, gate refuses
2. build → rename/copy the artifact (bytes intact) → `verified-current`
3. build → delete the sidecar → `unregistered`, gate refuses
4. build → advance the workspace and save → earlier artifact still `verified-current`
5. forged record with a mismatched hash → `modified-after-verify`
6. external Word edit → `unregistered` / `external-imported`, never `verified-current`
7. missing target → `missing`
8. a change between verify and publish → refused (no check/copy race)

## Boundaries with existing design

- **Workspace identity** (ADR 0046, `workspace_adopt()`): binds a DOCX to a
  *family*. It answers lineage, **not** content validity. The Gate must not
  take over that role, and `workspace_adopt()` must not gain verification
  duties.
- **External edits**: [foreign-edit.md](foreign-edit.md) already specifies
  `external edit → validate → re-extract → attribution → scope gate → adopt`.
  `foreign_edit_adopt()` is PRD-only; a legal Word edit becomes
  `external-imported` and earns a **new** trusted baseline — it never
  retroactively validates as the engine's byte-identical output.
- **Version / ADR 0044**: the Gate reuses `version_id` and `tree_sha256` from
  the delivery record; it does not re-derive history.

## Out of scope

- forgery resistance, protected publisher, signature verification;
- enforcing that *all* user-carried files are gated;
- retro-fitting delivery records onto the 173 pre-existing unattested files.
