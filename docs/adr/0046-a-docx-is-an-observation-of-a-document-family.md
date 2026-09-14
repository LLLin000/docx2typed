# 0046 — A DOCX is an observation of a document family

## Status

Accepted as a **v1 of the identity/resolution lane** (design 2026-09-14).

## Context

The engine had no answer to "which workspace is this file?". Every call had to
receive a workdir path, so the burden of remembering it fell on the caller, and
the natural user actions broke the association:

- a build output renamed by the user, or sent to a co-author and returned,
- a revision produced by editing the exported DOCX in Word,
- the same patent copied to start a new application from it.

Worse, the file-level facts that look like identity do not survive:

```
rename on one volume          file object stable
copy to another path          new file object
send by email / return        new file object, possibly new bytes
edit in Word and save         same file object, different bytes
save-as from the same source  docId / WPS hdid copied to the derived document
```

Two derived measurements decided the design:

- `docId`/`hdid` are inherited by documents saved from the same source — they
  **collide**, so they may nominate a candidate, never resolve one.
- content hash is exact but says nothing about *which* workspace an equal file
  belongs to: two projects started from one template share the bytes.

## Decision

Identity belongs to the **workspace**, not to any file. The model is:

```
Family f_…        permanent logical document (minted at first extract,
                  stored in <workdir>/workspace.json)
Workspace ws_…    one extraction's editing state; carries origin_family /
                  origin_version when it came from a fork
Observation       any DOCX seen for a family — a copy, a return, an export
```

1. `workspace.json` inside the workdir is authoritative; a workdir opened
   without ids mints them once (idempotent).
2. The resolver cache (`~/.docx2typed/registry.sqlite3`,
   `$DOCX2TYPED_REGISTRY` to relocate) is a **disposable index**. Deleting it
   must never damage a workspace or its history; the cost is one re-adoption
   question.
3. Resolution is evidence-graded, proof before continuity:

   | Tier | Evidence | Outcome |
   |---|---|---|
   | P1 | sha256 known for exactly one family | resolve |
   | P3 | local file object known **and** its hash known | resolve |
   | C1 | local file object known, content changed | ask once |
   | C2 | docId / hdid / created match | ask once (candidate only) |
   | — | two families claim the same bytes | ask once (ambiguous) |
   | U | nothing | unbound: create or choose |

4. **Nothing is ever adopted without proof or a confirmation.** A changed hash
   is not self-adopting: a path can keep its file id and hold another
   document's bytes, and a mail-returned copy is a new file object with the
   document's bytes.
5. Adoption carries the evidence it asked about: the token pins the content
   hash and the file object, so a file edited while the question was open is
   refused (`adoption-token-stale`) rather than bound.
6. `workspace_fork` starts a **new family** and keeps the origin for audit
   only. Afterwards the identical bytes are an ambiguous match — the caller is
   asked, never silently routed into the old family's history.
7. The engine registers what it produces: `extract` registers the source,
   `build_docx` registers its export after the commit publishes it (inside the
   transaction the file is not on disk yet).

## Consequences

- `workdir_open` accepts a DOCX and resolves it; the session descriptor answers
  with `family_id` / `workspace_id` / `origin_family`.
- A first call on an unbound document fails with an actionable status
  (`workspace-unbound` / `workspace-adoption-required` /
  `workspace-workspace-missing`) instead of guessing.
- A filesystem carrier (ADS/xattr holding the family id) is a designed tier but
  not implemented: it does not survive email or zip, so it can never become an
  authority — at most a hint on the same machine.
- The cache is local by design. Two machines each keep their own; a shared
  cache would need the evidence tiers re-checked for paths the other machine
  cannot see, which is exactly the guess this ADR refuses to make.
