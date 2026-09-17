# Diagnostics reference

Load this file after a structured refusal or when a writer may have changed the
workdir.

Recovery tool profiles share one invariant: a refusal may only name a lane the
active profile can actually enter. Both the `editor` and `review` profiles carry
the foreign-lane tools for this reason.

## Recovery order

1. Read the complete Result envelope and its `code`, `next_actions`,
   `capability`, `fallback`, `data.fix`, and `data` evidence.
2. If the result supplies `fresh_match_ref`, `divergence`, or a corrected hunk,
   use that exact recovery once with a fresh `operation_id`.
3. If the failure is stale state, re-open or re-read the workdir and carry the
   new `revision`/snapshot forward. Do not re-derive a long `old` string from
   memory.
4. A second refusal on the same paragraph is a blocker to report. Keep raw
   OOXML and private store mutation out of the recovery path.

Common safe responses:

| Signal | Response |
|---|---|
| stale document or match ref | re-read/search once, then use the fresh token/ref |
| revision-boundary refusal | narrow the edit to one editable span or choose a revision decision |
| style-region refusal | use the facade patch, not primitive region surgery |
| draft dirty at format/build | commit or revert the draft first |
| edit inside a pending insertion by another author (`edit-inside-pending-insertion`) | the edit would have to be recorded inside someone else's insertion, which this engine does not generate: accept that paragraph's insertion revision and edit it as body content, or make the change in Word. (An insertion's *own* author is absorbed in place — no refusal.) |
| trimmed historical Version | choose a retained Version; do not resurrect from a generation |
| operation-id-reused | omit the ID or generate a new one |
| native refusal whose recovery names `next: foreign-edit` | export a receipt-pinned candidate, change it externally, adopt it back ([`foreign.md`](foreign.md)) |
| `foreign-consent-required` | read the listed dependency, then retry once with the returned `consent_token` |
| `foreign-consent-stale` / `foreign-edit-conflict` / `foreign-out-of-scope` / `foreign-opaque-or-package-changed` | do not retry around it: re-prepare from the current clean Version and redo the external edit inside the target |
| `foreign-lineage-required` | ask the user which family and base Version the file belongs to; never let similarity answer |
| `workspace-unbound` | no lineage evidence; show `details.candidates` inventories, then ask whether to create a workspace or choose one — never guess |
| `foreign-identity-unproven` | the target rests on document-order pairing alone, which compares paragraphs without proving which base paragraph one is: widen the target to whole-document mode, or target a strongly-identified paragraph |
| `foreign-provenance-invalid` | the provenance record carries a field the engine does not record, is not self-consistent, or claims `engine-observed`; supply only `tool`/`version`/`binary_sha256?`/`argv`/`exit_code`, and accept that a tool identity here is caller-declared |
| `foreign-receipt-invalid` | a receipt-shaped file sits where a locator belongs, or the stored receipt no longer matches its own digests: re-prepare; never hand-edit engine-store state |

A successful browser display, queued event, or partial build is not delivery.
Delivery ends with a clean state, independent verify, and the promised Word
interoperability check.
