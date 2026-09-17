# Workspace reference

Load this file when a DOCX must be traced back to the workspace that owns it,
when the engine asks a lineage question, or when you need to start a new
document family from an existing file.

## One document family, many files

A workspace is **not** the DOCX. The engine's identity model is:

```
Family F1 ──┬── source.docx
            ├── 老师二审.docx        (managed exports of a version)
            ├── 邮件发回来的.docx     (an observation, re-bound by adoption)
            └── 专利-新申请/          (a fork: new family, origin recorded for audit)
```

- **Family** — the permanent logical document (`f_…`), minted when a workdir is
  first extracted, stored in `<workdir>/workspace.json`.
- **Workspace** — the on-disk editing state (`ws_…`), one per extraction,
  carrying `origin_family`/`origin_version` when it came from a fork.
- **Observation** — any DOCX the engine has seen for a family. Files are
  copies, renames, or returns of the same document; the workspace is the
  authority on what that document currently is.

`workspace.json` is authoritative and lives inside the workdir. The resolver
cache (`~/.docx2typed/registry.sqlite3`, override with `$DOCX2TYPED_REGISTRY`)
is only a **lookup index**: deleting it never damages a workspace or its
history. The cost of a cold cache is one re-adoption question.

## `workdir_open` accepts a DOCX

`workdir_open(<path>)` takes a workdir *or a DOCX*. Give it a document and the
engine resolves the family and opens that family's workspace:

```
workdir_open("老师二审.docx")   → opens the workspace that owns it
```

`workdir_open` answers with the permanent ids (`session.workspace.family_id`,
`session.workspace.workspace_id`, `origin_family`), so a caller never has to
pass a path around to stay on the same document.

## The evidence ladder

Resolution runs strongest-evidence-first. **Proof resolves; continuity asks.**

| Tier | Evidence | Outcome |
|---|---|---|
| P1 | sha256 seen for exactly one family | resolve, no question |
| P3 | this local file object is known **and** its content hash is known | resolve, no question |
| C1 | this local file object is known, content changed | ask once — "same file, new content" |
| C2 | docId / WPS `hdid` / created hints match | ask once — candidate only |
| — | two or more families claim the same bytes (a fork) | ask once — ambiguous |
| U | nothing | `workspace-unbound`: report known workspace candidates, then create or choose |

Why a changed hash is never adopted automatically: a file object is a fact
about *this machine's filesystem*, not about the document. A path can be
rewritten with a different document's bytes while keeping its file id, and a
copy sent by email arrives as a new file object with the document's bytes.
"Same bytes" is proof; "same file" is only a question.

## Answering the question

For `workspace-unbound`, the diagnostic includes the known local workspaces so
the caller can make an informed choice:

```json
{"code": "workspace-unbound",
 "details": {"reason": "no-evidence",
             "sha256": "…",
             "candidates": [{"family_id": "f_…", "workspace_id": "ws_…",
                             "status": "resolved",
                             "inventory": {"paragraphs": 51, "media": 1}}],
             "actions": ["create-workspace", "choose-existing-workspace"]}}
```

Counts are best-effort observations of the recorded workdir. A missing or
invalid workdir reports `null` counts and still requires an explicit choice.

`workdir_open` refuses with `workspace-adoption-required` and returns:

```json
{"code": "workspace-adoption-required",
 "details": {"reason": "same-local-file-object",
             "sha256": "…",
             "candidates": [{"family_id": "f_…", "previous_version": "V12",
                             "evidence": {"same_file_id": true, "known_exact_hash": false}}],
             "adoption_token": "eyJ…"}}
```

Ask the user **one** lineage question, then record the answer:

```
workspace_adopt(token=<adoption_token>, family_id=<chosen candidate>)
```

The token pins the content hash and the file object, so a file edited while
the question was on screen is refused (`adoption-token-stale`) instead of
adopted by accident. After adoption the same bytes resolve with no question.

## Starting a new family from an existing file

```
workspace_fork(docx="旧专利.docx", outdir="新申请/")
```

Extraction runs the same lane as `extract`; the new workdir gets a **new**
family and records `origin_family`/`origin_version` for audit only. From then
on the identical bytes are an ambiguous match, so the caller is asked (or
passes the workspace explicitly) rather than silently landing in the old
family's history.

## Resolution statuses

| Status | Meaning |
|---|---|
| `resolved` | exactly one family; the workdir is usable |
| `family-known-but-workspace-missing` | family known, recorded workspace not at its path (`workspace-workspace-missing`) — locate it, or start a new workspace for the family |
| `adoption-required` | proof is short; ask the one question |
| `unbound` | nothing known: `create-workspace` or `choose-existing-workspace`; known local candidates include best-effort paragraph/media counts |

## What this does not do (yet)

- **No managed workspace root.** `extract` still writes where `-o` says; the
  registry only records where a workspace lives.
- **No filesystem carrier.** An ADS/xattr carrying the family id (so a copy
  *across* machines stays self-describing) is a designed tier, not implemented;
  the P2 row above is reserved for it and stays off by default because it does
  not survive email/zip and must never become an authority.
- **Metadata hints are hints.** `docId`/`hdid` were measured to collide between
  documents saved from the same source, so C2 can only nominate candidates.
- `decide_all`'s decided output is not registered automatically; `build_docx`
  exports are registered with their DOCX lineage hints.
