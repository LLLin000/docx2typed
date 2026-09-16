# Foreign reference

Load this file when the requested change is one the canonical model cannot
express — a style definition, page setup, a drawing, a field, or container
structure the native lane refuses — and the work has to happen in Word, WPS,
LibreOffice, OfficeCLI, or a user script.

## Route

```text
native refusal names next: foreign-edit
→ foreign_edit_prepare(target)        # engine exports a pinned Version
→ external tool edits that candidate  # never the live workdir
→ foreign_edit_adopt(candidate)       # re-extract, attribute, gate, adopt
```

The engine owns provenance, freshness, attribution, the gates, and the
adoption. It does not run the external tool: there is no generic subprocess
surface, and none should be improvised around one.

## Prepare

- Requires a clean committed state; a dirty draft or version refuses
  (`foreign-base-not-clean`). Save first.
- Export one saved Version (`version=` defaults to HEAD). The authoritative
  receipt goes into the engine store
  (`.docx2typed-store/foreign-candidates/<FC>.json`): family, workspace, base
  Version, commit, tree, base export hash, target, anchor set. The file beside
  the candidate is a locator with the candidate id only — adoption reads the
  store copy, so editing files next to the candidate cannot forge lineage.
- The candidate must be outside the live workdir (`foreign-candidate-invalid`).
- Derive the target from the request, not from a change-type list:

```text
["paragraph:P12"] ["paragraph:P12..P20"] ["table:T2"] ["style:Heading1"] ["section:0"]
omitted → whole-document mode (an explicit "我手工改过了，收下这个文件")
```

## Adopt

- With a receipt, the base Version comes only from the receipt. Without one,
  manual entry requires an explicit `family_id` and `base_version`; the
  resolver may nominate, never select, and similarity never decides.
- Attribution is deterministic: stable external ID → whole-ID-set paragraph
  ID → structural identity → exact structural sequence. Repeated identity with
  no deterministic mapping refuses (`foreign-identity-ambiguous`).
- Order pairing reports changes but is never an identity proof: a narrow
  target whose paragraph (or insertion position) is only order-paired refuses
  (`foreign-identity-unproven`); whole-document mode still accepts it.
- Layers: semantic, format, opaque, package (inventory, relationships, content
  types), and normalization (proven semantic-equivalent serialization churn).
- A style-definition change that the target names is one confirmation
  (`foreign-consent-required`, retry with the returned `consent_token`).
- Refuse and do not retry around it: a change outside the target
  (`foreign-out-of-scope`), lost or changed opaque/package content
  (`foreign-opaque-or-package-changed`, `foreign-opaque-changed`), HEAD having
  left the receipt's base (`foreign-edit-conflict`), and any candidate/receipt/
  consent drift (`foreign-consent-stale`). Consent never overrides these.
- Success is an ordinary next Version (`origin: baseline-transition`): same
  family, same timeline, re-rooted baseline. Report it as one version, not as
  a new document.

## Evidence

The Version records the candidate id, receipt hash, target, decision, the
attribution changes, and the normalization footprint. A candidate with no
semantic or proven normalization delta refuses (`foreign-candidate-noop`).

For layout and style work a text diff is not what the user accepts: OfficeCLI
HTML/screenshot and LibreOffice PDF are visual evidence only, and `verify_output`
plus the attribution report stay the structural authority.
