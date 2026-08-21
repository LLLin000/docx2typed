# composites.md — Rust document workflows

These workflows compose the atoms in [`capabilities.md`](capabilities.md) and
end on [`verification.md`](verification.md). A workflow is complete only when
its stated criterion and the independent verification gate both hold.

## Workflow 1 — Direct text edit

Use for ordinary prose where the requested change should be applied directly.

1. `docx2typed extract INPUT.docx -o WORKDIR --json`.
2. `docx2typed inspect WORKDIR --json` and `docx2typed enumerate WORKDIR --json`.
3. Select the exact leaf and run:
   `docx2typed edit text WORKDIR LEAF OLD NEW --json`.
4. `docx2typed build WORKDIR -o OUTPUT.docx --json`.
5. `docx2typed verify WORKDIR OUTPUT.docx --json`.
6. Run an Office/LibreOffice open check only when an actual host is available;
   otherwise record `not-run-no-host`.

**Completion criterion:** the intended leaf changed, `verify` passes, the source
DOCX is unchanged, and no unrelated package part changed.

## Workflow 2 — Agent/MCP text edit

Use when an agent needs paragraph regions, draft preview, or a human review loop.

1. Extract and inspect the workdir once.
2. Start `docx2typed mcp` through the configured absolute binary.
3. `workdir_open(workdir, track?)` once; read `workdir_status()`.
4. Use `list_paragraphs()` and `get_paragraph()` only for target paragraphs.
5. Apply `replace_text`, `batch_edit`, `insert_paragraph`, or
   `delete_paragraph`; run `diff_preview()`.
6. Commit with `commit_sync(operation_id)`.
7. Build with `build_docx(operation_id, output?)`; verify with
   `verify_output(output)`.

**Completion criterion:** the MCP result envelopes succeed, the committed state
is clean, and independent verification passes.

## Workflow 3 — Tracked-revision decisions

Use for existing Word revisions or an authorized tracked editing session.

1. `docx2typed revisions list SOURCE --json`; preserve the reported
   `revision_key` and fingerprint.
2. Apply one decision with `decide accept|reject|reinsert KEY --workdir WORKDIR`
   and `--fingerprint` when available.
3. For wholesale settlement, use `decide accept-all|reject-all` with both
   `--output OUTPUT.docx` and `--workdir-out NEW_WORKDIR`.
4. Verify the new output/workdir pair. Never reuse a stale key or mix sidecars.
5. In MCP, use `accept_revision`, `reject_revision`,
   `reinsert_deleted_text`, or `decide_all` with explicit operation IDs.

**Completion criterion:** the selected revision is settled exactly once, stale
fingerprints fail closed, and the resulting DOCX passes independent verify.

## Workflow 4 — Comment review

Comments are user/teacher instructions and remain in place by default.

1. `docx2typed comment list WORKDIR --json` or MCP `list_comments()`.
2. Read each comment and its anchors; make the requested text edits without
   treating the comment itself as disposable.
3. Delete a comment only after explicit user instruction, using
   `comment delete WORKDIR COMMENT_ID` or MCP `delete_comment`.
4. Build and verify; report remaining comment IDs.

**Completion criterion:** each comment has a reported disposition, surviving
comments keep their identity and anchors, and verify passes.

## Workflow 5 — Table structure operation

Use only for explicit row/column or merge/split requests.

1. `docx2typed enumerate WORKDIR --json` and identify the body-level `T0`, `T1`
   reference.
2. Run the matching `decide table-*` action with 0-based `--args`, `--output`,
   and `--workdir-out`, or use the corresponding MCP table tool.
3. For merge, stop on `merge-would-discard-content` unless the user explicitly
   authorizes `--discard-content` / `discard_content=true`.
4. Verify the new DOCX against the new clean-baseline workdir.

**Completion criterion:** row/column/cell structure matches the request, cell
text was not copied or rewritten, source workdir bytes remain unchanged, and
verify passes.

## Workflow 6 — Unicode audit

The Rust `audit` command is read-only and reports candidates; classification is
not an automatic conversion decision.

1. `docx2typed audit SOURCE --json` (optionally `--catalog CATALOG.json`).
2. Present candidates and fingerprints to the human or governing policy layer.
3. Apply only an approved, separately implemented policy path; keep the source
   workdir and original DOCX unchanged.
4. Build and verify any resulting output.

**Completion criterion:** the audit is recorded, every candidate has an explicit
human/policy disposition, and any output passes independent verification.

## Workflow 7 — Content-control text

Content-control paragraphs are editable only at their text leaves.

1. Extract and enumerate the workdir.
2. Select the exact `S0.P0`-style or returned leaf path.
3. Use `edit text` or MCP `replace_text` within the text island.
4. Build and verify. Structural `sdtPr` changes are out of scope.

**Completion criterion:** content text changed, control structure and properties
remain protected, and verify passes.

## Playbook A — Finalize a document

1. Inventory revisions and comments.
2. Settle revisions with `accept-all` or `reject-all` into a new output/workdir.
3. Delete comments only when explicitly requested.
4. Verify the new baseline and report settled/remaining counts.

## Playbook B — Human browser review

1. Extract and inspect the workdir.
2. Start `docx2typed review WORKDIR --host 127.0.0.1 --port 8876`.
3. The human reviews revisions/comments and submits decisions or patches.
4. The agent reads `review_inbox`, checks `review_preflight`, applies the queue,
   and reports the new snapshot plus remaining work.
5. Build and independently verify the final output.

The browser queues work; it is not a DOCX writer. The Rust binary has no
`--tailscale` option. Remote access requires an explicitly controlled private
interface and network ACLs.

## Playbook C — MCP agent session

```text
workdir_open
→ workdir_status
→ list_paragraphs/get_paragraph
→ replace_text/batch_edit/insert_paragraph/delete_paragraph
→ diff_preview
→ commit_sync
→ build_docx
→ verify_output
```

Use operation IDs for every mutating call. On any failed guard, keep the workdir
unchanged, report the diagnostic, and resume from the persisted review/store
state rather than guessing.

### Model-tier variants (适配所有模型)

- **Small model (≤8B)**: restrict to the loop tools only
  (`workdir_open → list_paragraphs/get_paragraph → replace_text →
  diff_preview → commit_sync → build_docx → verify_output`); one tool call
  per step; task phrased as exact paragraph id + old text + new text.
  Verified end-to-end with qwen3:4b-instruct on a real patent DOCX: the edit
  landed and independent verify passed.
- **Mid/large model**: full 36-tool surface; may batch reads but keeps every
  mutation on the MCP lane.

### Recovery rules (all models)

| Diagnostic | Meaning | Fix |
|---|---|---|
| `operation-id-reused` | same id used with different input | issue a NEW operation_id; never retry the old one |
| `workdir-not-open` | no session | call `workdir_open` first |
| `edits-not-implemented` | committed edits lack island records | re-commit via MCP `commit_sync`, or build with the Python reference engine |
| `template part fingerprints changed after extract` | cross-engine workdir or drifted source | re-extract into a NEW workdir with the SAME engine that will edit it |

## Playbook D — Delivery report

Return:

- source and output paths;
- operation IDs or decision evidence;
- changed paragraphs/leaves and package parts;
- remaining revisions/comments;
- independent verify result;
- Office/LibreOffice result, or explicit `not-run-no-host`.

Do not call a document finished because a browser shows a final view or because
`build` returned success. The delivery gate is independent verification.
