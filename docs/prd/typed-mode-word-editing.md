# PRD: Word-like clean editing for typed mode

> **Status:** implemented. `edit status` / `edit refresh` / `edit sync` shipped in commits `b311c02` (Slice A) and the Slice B+C implementation; dirty prose synchronization applies the Word-like ownership policy below.

## Problem Statement

The current typed-mode source is safe for DOCX round-tripping but is still a poor Agent editing surface. A normal paragraph exposes formatting boundaries as inline spans such as `<span data-s="s_...">...</span>`. Those spans encode Word run structure rather than writing intent. An Agent that rewrites a sentence must either preserve opaque style tags while changing prose or trigger the current cross-boundary validator, which rejects edits when style ownership cannot be proven.

The existing clean projection removes the visual noise, but it is read-only. It therefore solves human inspection, not the actual file-edit workflow an Agent uses. Adding more instructions to the skill cannot make a span-heavy source as easy to edit as the legacy plain-text source.

Word provides a formatting context at an insertion point, but the exact behavior is not one universal paragraph-wide rule. The context can depend on the destination paragraph, the character immediately preceding the caret, the first character at paragraph start, a selected range being replaced, and formatting carried by spaces. Microsoft documents some of these behaviors for paste operations; other caret cases are project-defined and must be verified against fixtures rather than presented as Word internals.

The project therefore needs a deliberate text-editing layer that:

- gives the Agent a plain, continuous prose file with stable paragraph identity;
- maps the resulting text edit back to the typed AST;
- assigns styles according to an explicit, conservative inheritance contract;
- preserves existing structural tokens, relationships, package bytes, and validator guarantees;
- fails closed when the edited text cannot be mapped safely; and
- records enough evidence to explain every style assignment and synchronization result.

## Solution

Add a hash-bound `edit.md` projection and a governed text-edit synchronizer to the typed workdir.

`typed.md` remains the canonical restricted typed source. It retains style spans and structural tokens for serialization, debugging, exact structure edits, build, and verification. `edit.md` is the generated Agent-facing draft. It contains continuous paragraph text, stable paragraph markers, explicit deletion markers, and only the minimum protected placeholders required for non-text DOCX structures. It never exposes character-style spans, style IDs, raw `rPr`, or XML formatting in ordinary prose.

The workflow becomes:

```text
source DOCX
  -> extract typed workdir
  -> typed.md + edit.md projection
  -> Agent edits prose in edit.md
  -> edit sync edit.md into the typed AST and typed.md
  -> build output DOCX
  -> independent verify
```

Raw typed editing remains an advanced path:

```text
edit typed.md directly
  -> validate typed.md
  -> edit refresh
  -> build output DOCX
  -> independent verify
```

`sync` is the only mutation seam for clean text editing. It compares each edited paragraph with the text projected from the current canonical typed source, computes a deterministic edit script over protected text units, preserves style ownership for unchanged text, assigns styles to inserted text using the policy below, validates the resulting AST, writes run evidence, and updates the projection. It never guesses through a stale or ambiguous input.

The edit header binds the draft to both the canonical source and the projection that the Agent received:

```text
<!--@edit schema="1" sync-contract="1"
     base-typed-sha256="..."
     base-projection-sha256="..."
     segmentation="uax29-c1-1/unicode-16.0.0" -->
```

The header is a single grammar object; line wrapping above is illustrative. The runtime also computes the current edit-body hash. These values define the freshness state:
`base-typed-sha256` is the SHA-256 of the exact canonical `typed.md` bytes.
`base-projection-sha256` is the SHA-256 of the canonical `edit.md` body with
the header excluded and declared line-ending normalization applied. Runtime
comparison uses the same canonicalization; it must not hash a self-referential
header or silently compare platform-specific CRLF/LF bytes.


| Current `typed.md` | Current edit body | State | Allowed action |
| --- | --- | --- | --- |
| equals `base-typed-sha256` | equals `base-projection-sha256` | `clean` | build or no-op sync |
| equals `base-typed-sha256` | differs from `base-projection-sha256` | `dirty` | validate and sync |
| differs from `base-typed-sha256` | equals `base-projection-sha256` | `stale-clean` | refresh projection; no edit was lost |
| differs from `base-typed-sha256` | differs from `base-projection-sha256` | `conflict` | reject; require explicit user resolution |

`build` and `validate` reject `dirty`, `stale-clean`, and `conflict` states. There is no `build --ignore-edit` escape hatch. After a successful clean sync, `edit.md` is regenerated from the new canonical `typed.md` and returns to `clean`.

This PRD is an addendum to the existing typed-mode v2 PRD. It replaces the v2 decision that automatic cross-span visible-text diffing is out of scope, but it does not relax package-integrity, structure-skeleton, baseline-drift, opaque-node, transactional-build, or independent-verify contracts. The clean-sync decision and its supersession of the old cross-span rule are recorded in ADR 0036.

## User Stories

1. As an AI editor, I want the default editing file to contain continuous prose without style spans, so that I can rewrite language without manipulating Word implementation details.
2. As an AI editor, I want each editable paragraph to retain a stable ID marker, so that a text change can be attributed to one paragraph without relying on line numbers.
3. As an AI editor, I want the edit file to declare its typed-source and projection fingerprints, so that I cannot unknowingly edit a stale representation.
4. As an AI editor, I want ordinary ampersands, angle brackets, whitespace, and Unicode characters to remain literal and round-trip unchanged, so that scientific and patent text is not silently normalized.
5. As an AI editor, I want a pure insertion in the middle of a paragraph to inherit the effective style of the character immediately to its left, so that newly written text has a defined caret context.
6. As an AI editor, I want an insertion at the start of a paragraph to use the first visible character's style on its right, so that paragraph-start insertion has a deterministic project policy.
7. As an AI editor, I want an insertion at the end of a paragraph to inherit the final visible text unit's style, so that appended text follows the existing typing context.
8. As an AI editor, I want text inserted after a formatted space to inherit the space's style, so that invisible character formatting is not lost merely because the space looks plain.
9. As an AI editor, I want a replacement wholly inside one style region to retain that region's style, so that rewriting a bold or italic phrase does not flatten it.
10. As an AI editor, I want a local replacement spanning several style regions to be accepted only when an unchanged anchor and unique alignment make ownership safe, so that a mixed-style warning is not mistaken for silent inference.
11. As an AI editor, I want unchanged text around a rewrite to keep its original styles, so that a local wording change does not reformat the rest of the paragraph.
12. As an AI editor, I want a deletion to remove only the selected visible text and leave surviving style boundaries valid, so that deleting words cannot damage adjacent formatting.
13. As an AI editor, I want an empty paragraph to use its recorded insertion style, distinct from its ordinary base style when Word stores paragraph-mark formatting, so that inserting into an empty paragraph has a defined result.
14. As an AI editor, I want paragraph text to remain separate from paragraph formatting, so that ordinary prose edits do not change indentation, numbering, alignment, spacing, or section properties.
15. As an AI editor, I want new paragraphs to use explicit inheritance from an existing paragraph, so that paragraph-level and insertion styles are copied intentionally rather than guessed.
16. As an AI editor, I want paragraph deletion to require an explicit tombstone, so that deleting a block in the edit file cannot silently delete DOCX content.
17. As an AI editor, I want structural placeholders to retain stable IDs and be protected from ordinary prose edits, so that comments, bookmarks, hyperlinks, tabs, breaks, and opaque nodes are not moved or recreated accidentally.
18. As an AI editor, I want any visible-text change in a paragraph containing an unsupported opaque node to fail clean sync, even when the placeholder itself was untouched, so that incomplete XML is never synthesized.
19. As an AI editor, I want an edit that moves, deletes, duplicates, or changes a protected token to fail with the exact paragraph and token ID, so that I can repair the draft without inspecting the whole DOCX.
20. As an AI editor, I want repeated words and repeated paragraphs handled deterministically when all valid alignments have equivalent ownership, while genuinely different ownership choices fail closed.
21. As an AI editor, I want a mixed-style full-paragraph rewrite with no unchanged anchor to fail by default, so that the system does not invent a style for lost structure.
22. As an AI editor, I want unformatted pasted content represented by the same destination-context policy as clean text edits, so that I do not need to author source formatting tags.
23. As an AI editor, I want source-format-preserving copy/paste to remain an explicit advanced operation, so that the clean edit surface does not pretend to know formatting that is absent from plain text.
24. As an AI editor, I want `sync` to be idempotent, so that applying an unchanged edit draft does not create new spans or alter XML.
25. As an AI editor, I want a failed pre-commit sync to leave the previous typed source and projection untouched, so that malformed or ambiguous rewrites cannot destroy the last valid state.
26. As an AI editor, I want a projection-refresh failure after canonical commit to be represented as stale or missing rather than hidden, so that the next build cannot consume an unrefreshed draft.
27. As an AI editor, I want build to detect an edited but unapplied `edit.md`, so that I cannot accidentally build an older typed source while believing my prose change was included.
28. As an AI editor, I want raw typed editing to remain available through an explicit `edit refresh` path, so that clean editing does not remove the escape hatch for exact structure-aware maintenance.
29. As an AI editor, I want diagnostics to distinguish stale state, malformed edit grammar, protected-token mutation, opaque-paragraph mutation, ambiguous alignment, mixed-style policy, and projection-refresh failure, so that each failure has an actionable remedy.
30. As a maintainer, I want clean editing and raw typed editing to converge on the same AST, so that parser, validator, builder, and verifier do not implement two incompatible document models.
31. As a maintainer, I want the style registry to remain immutable during clean text editing, so that Word-like inheritance reuses existing styles without inventing unverified formatting XML.
32. As a maintainer, I want the synchronizer to preserve the existing hybrid-fidelity contract, so that untouched paragraphs still use byte replay and only text-touched paragraphs are synthesized.
33. As a maintainer, I want every style assignment to be explainable by an edit hunk, caret context, alignment identity, and protected-boundary decision, so that a formatting discrepancy can be diagnosed from a small report.
34. As a maintainer, I want each sync to be a first-class provenance event bound to the input hashes, output hash, changed Paragraph IDs, and hunk report, so that a later build can identify why a style was assigned.
35. As a maintainer, I want a reference fixture corpus derived from Word documents with mixed styles and boundary edits, so that the project tests observable behavior rather than assumptions about spans.
36. As a maintainer, I want the synchronizer to reject edits that would change package relationships, protected XML, or template fingerprints, so that a convenient text interface cannot weaken document safety.
37. As a maintainer, I want the CLI and skill guidance to make `edit.md` the default Agent entry point only after the sync contract is implemented, so that documentation never advertises an unimplemented mutation path.

## Implementation Decisions

- **Canonical versus Agent-facing source:** `typed.md` is the canonical serialized typed AST. `edit.md` is a generated projection and patch input. It is never merged by line position alone and never becomes a second editable truth. `format.json`, `styles.json`, `_template.docx`, package manifests, and run records remain read-only from the clean-edit surface.
- **Edit grammar:** `edit.md` uses a project-owned restricted grammar, not CommonMark or generic HTML. Marker lines are not prose. Existing paragraph markers, deletion tombstones, and new-paragraph declarations are parsed structurally; each existing paragraph body is one logical source line.
- **Concrete grammar example:**

  ```text
  <!--@edit schema="1" sync-contract="1" base-typed-sha256="..." base-projection-sha256="..." segmentation="uax29-c1-1/unicode-16.0.0"-->
  <!--@p id="P3"-->
  普通正文与 A < B、R&D。⟦range-start kind="hyperlink" id="H1"⟧此处⟦range-end id="H1"⟧⟦token kind="tab" id="T1"⟧继续。
  <!--@new temp="N1" inherit="P3"-->
  新段落。
  <!--@delete id="P4"-->
  ```

  `@p`, `@new`, `@delete`, and `@edit` markers have reserved positions. A body newline is a source-file line ending, not a Word paragraph break. A tab, line break, drawing, or other non-text item is an atomic `⟦token ...⟧`; a paired editable range uses `⟦range-start ...⟧` and `⟦range-end ...⟧`. Only known kinds and attributes are accepted. Literal `⟦` and `⟧` in prose use the grammar escapes `\u27E6` and `\u27E7`; the parser does not silently reinterpret malformed markers. Ordinary `<`, `>`, `&`, spaces, and non-ASCII text remain literal.
- **Protected structure:** A protected token or range must remain present exactly once, in the same paragraph, with the same ID, kind, attributes, nesting, and relative order. It cannot be created, deleted, moved, duplicated, or retargeted by clean sync. A paragraph containing an unsupported or opaque node is immutable for clean sync: any visible-text change in that paragraph is rejected, even if the opaque placeholder itself was not touched. This prevents a partial paragraph synthesis from invalidating unknown XML.
- **Paragraph identity and order:** Existing Paragraph IDs remain stable. Existing paragraphs cannot be silently reordered or omitted. Deletions require tombstones. New paragraphs use `<!--@new temp="N1" inherit="P3"-->`; sync allocates the formal Paragraph ID and regenerates `edit.md`. New paragraph text uses the inherited paragraph's `insertion_style_id`, not an arbitrary first style.
- **Text units:** Synchronization uses Unicode extended grapheme clusters under UAX #29 conformance clause UAX29-C1-1, pinned for this contract to Unicode 16.0.0, plus protected tokens as atomic units. It must not split combining sequences, emoji ZWJ sequences, flag sequences, tabs, breaks, placeholders, or CRLF-like source sequences. The segmentation contract and Unicode version are recorded in the edit header and sync evidence.
- **Diff mapping:** Each paragraph is diffed independently against canonical visible text. Equal units retain the baseline style attached to the corresponding unit. Insertions, replacements, and deletions produce a hunk report. The synchronizer enumerates the minimum-cost alignment candidates needed to determine whether ownership is unique; if candidate alignments differ in style ownership, protected-token mapping, or changed ranges, it fails with `ambiguous-alignment`. If all candidates are ownership-equivalent, a documented deterministic tie-break may choose one and records `alignment-tie-resolved`.
- **Word-derived versus project-defined policy:** Microsoft documents that Merge Formatting and Keep Text Only use the destination paragraph style plus direct formatting or character-style properties immediately preceding the cursor. The project derives the left-context insertion rule and formatted-space behavior from that evidence. The paragraph-start right-context rule, empty-paragraph fallback, mixed-selection tie-break, and ambiguity rejection are project-defined deterministic safety rules, not claims about an undocumented Word universal.
- **Word-like style policy:**

  | Edit hunk | Style assigned to inserted text |
  | --- | --- |
  | Pure insertion at paragraph offset 0 with visible text to the right | Style of the first visible unit to the right |
  | Pure insertion at any other caret position | Style of the visible unit immediately to the left, including a formatted space |
  | Pure insertion into an empty paragraph | `insertion_style_id` |
  | Replacement wholly inside one effective style | That selected range's style |
  | Local mixed-style replacement with an unchanged anchor on at least one side, no protected boundary, and unique alignment | Style at selection start; emit a warning and hunk evidence |
  | Mixed-style replacement with no unchanged anchor or with different valid ownership choices | Reject with `mixed-selection-requires-anchor` or `ambiguous-alignment` |
  | Replacement adjacent only to protected structure | Use the nearest recorded text context only when unique; otherwise reject |
  | Deletion with no inserted text | Preserve styles of all surviving units; normalize empty and adjacent equivalent nodes |

  A full-paragraph rewrite with no unchanged anchor is accepted only when the original paragraph has one effective character style. If the original paragraph has multiple effective styles, the rewrite is rejected by default as `unanchored-mixed-rewrite`; an explicit future policy may reopen this, but ordinary clean sync never chooses a style silently. The policy models ordinary typing and unformatted/merge-formatting paste. It does not reproduce Keep Source Formatting paste.
- **Caret context:** A space is a real styled text unit. The synchronizer never trims it before choosing context. At paragraph start it uses the right context; elsewhere it uses the left context. If no visible character exists, it uses `insertion_style_id`.
- **Empty-paragraph insertion style:** `insertion_style_id` is separate from `base_style`. Extraction records the paragraph-mark `w:rPr` when present; otherwise it follows the validated project fallback for the empty-run typing context, then the paragraph base style. The priority is fixture-tested and versioned. It is used for empty-paragraph insertion and inherited new paragraphs; visible-text majority style is not a substitute.
- **Mixed replacements:** Mixed-style replacement is rejected by default. The controlled exception requires an unchanged visible anchor on at least one side, a unique protected-token-preserving alignment, no opaque paragraph, no protected-boundary crossing, and a finite selected range. It uses the selection-start style only as an explicit project tie-break, emits a warning, and records the reason. It never silently flattens an unanchored mixed paragraph.
- **Style registry:** Clean editing may reference only existing style IDs in the immutable registry. No new style is synthesized, and no existing style's XML, label, or ID is changed. Formatting changes remain a separate governed capability.
- **Structural skeleton:** Style boundaries are no longer an input burden for ordinary text rewrites, but the resulting AST must contain the same protected token IDs, range nesting, relationships, and paragraph properties. Empty spans and adjacent equivalent styles may be normalized in memory after successful ownership assignment.
- **Freshness state:** The edit header records `edit_schema`, `sync_contract`, `base_typed_sha256`, `base_projection_sha256`, and `segmentation`. Runtime state also computes current `typed.md` and edit-body hashes. `clean`, `dirty`, `stale-clean`, and `conflict` are first-class diagnostics and build gates; a single typed hash is insufficient.
- **Raw editing and refresh:** Raw typed editing follows `typed.md -> validate -> edit refresh -> build`. `edit refresh` is allowed when the edit body is clean and typed changed. If both typed and edit changed, it reports `conflict`; `--discard` is required to throw away dirty edit content. Build never ignores a dirty or stale projection.
- **Sync provenance:** Every sync writes a run record containing `command`, `actor`, `sync_contract_version`, `edit_schema_version`, `segmentation`, `typed_before_sha256`, `edit_base_projection_sha256`, `edited_edit_sha256`, `typed_after_sha256`, `changed_paragraph_ids`, `hunk_report_sha256`, diagnostics, and status. Each hunk records Paragraph ID, baseline/new ranges, operation, alignment identity, source style set, assigned style, assignment reason, protected boundaries, and warning/error. A later build binds to `typed_after_sha256` and the sync run.
- **Transactional synchronization:** Parse, freshness checks, protected-token validation, diff, AST validation, hunk evidence, and rendering happen before canonical commit. The implementation stages both complete files and writes evidence before replacement. If validation, rendering, or the first file replacement fails, both existing files remain byte-identical. If the process is interrupted after canonical `typed.md` replacement but before projection replacement, `typed.md` remains the committed canonical state and `edit.md` is stale or missing; the next command must detect this and `edit refresh` repairs it. The contract does not claim two independent filesystem replacements are one atomic transaction.
- **CLI seam:** Extraction generates both files; `edit refresh` regenerates the projection after a raw typed change; `edit sync` applies the clean draft; `view --mode raw` remains the precise structural inspection path; `build` and `verify` consume the typed workdir only after freshness gates pass. There is no `build --ignore-edit`.
- **Diagnostics:** Every applied hunk records Paragraph ID, old range, new range, operation type, baseline style context, assigned style, alignment identity, and warning. Errors include the paragraph ID and smallest offending token/range available. Stable codes include `edit-stale`, `edit-dirty`, `edit-conflict`, `protected-token-mutated`, `opaque-paragraph-mutated`, `mixed-selection-requires-anchor`, `unanchored-mixed-rewrite`, `ambiguous-alignment`, and `projection-refresh-failed`.
- **Compatibility with v2 safety:** Byte-preserving document patching, template/package fingerprints, opaque-node restrictions, baseline drift rejection, paragraph structure rules, transactional output, independent verify, and hybrid fidelity remain unchanged. The clean-sync seam is the only governed exception to the previous no-cross-span-diff rule.

## Testing Decisions

- Tests defend observable editing behavior and document/package invariants, not private helper names, regexes, or AST class layouts.
- **Slice A — projection and state:** extraction creates `edit.md`; the grammar parser accepts the header and markers; the four freshness states are derived from hashes; no-op sync is idempotent; raw typed edits refresh only when safe; dirty/stale/conflict drafts block build; sync run evidence is complete. No complex style diff is needed in this slice.
- **Slice B — safe text edits:** the primary seam is `extract -> edit.md -> edit sync -> build -> independent verify`. Fixtures cover pure insertion, deletion, one-style replacement, empty paragraphs with `insertion_style_id`, formatted spaces, paragraph start/end, repeated text with ownership-equivalent alignment, and protected-token preservation.
- **Slice C — controlled mixed edits:** fixtures cover a local mixed-style replacement with one unchanged anchor, the warning and selection-start style, missing-anchor rejection, full mixed-paragraph rejection, protected-boundary rejection, and genuinely ambiguous alignment. Every accepted hunk has deterministic evidence.
- The minimum mixed-style fixture contains plain text, bold text, italic text, a differently sized/font-styled region, formatted spaces, an empty paragraph, paragraph-mark formatting, and adjacent equivalent runs that should canonicalize together.
- Unicode cases use UAX29-C1-1 grapheme clusters pinned to Unicode 16.0.0 and cover non-ASCII prose, combining sequences, emoji ZWJ sequences, flag sequences, XML entities, leading/trailing spaces, tabs, and explicit line-break tokens. The result preserves logical units and `xml:space` behavior.
- Structural cases cover hyperlinks, comments, bookmarks, tabs, breaks, opaque nodes, range nesting, tables outside the editable surface, paragraph insertion, paragraph deletion, and protected-token movement. Any visible edit in an opaque paragraph must fail, not merely a placeholder mutation.
- Stale-state tests independently change typed source, edit body, and both sides after projection generation, then assert clean/dirty/stale-clean/conflict classification and safe actions. A projection-refresh interruption must leave a detectable stale state.
- Failure-path tests prove no final DOCX is published for malformed edit grammar, duplicate paragraph IDs, missing tombstones, changed protected placeholders, style registry mutation, ambiguous alignment, wrong template, source drift, and package-integrity violations.
- Provenance tests assert sync before/after hashes, changed Paragraph IDs, hunk report hash, style-assignment reason, segmentation contract, actor, and the later build's binding to the sync output.
- Idempotence tests run sync twice, build twice, and verify both outputs. The second pass must produce no new text or style changes and must preserve the package manifest contract.
- A Word reference matrix distinguishes documented paste behavior from project-defined caret rules. It covers start insertion, left-context insertion, formatted-space insertion, end insertion, single-style replacement, local mixed replacement, and full mixed rewrite rejection. CI may use committed fixtures; it need not launch Word on every platform.
- LibreOffice rendering may be used as an additional smoke check, but rendered pagination or font substitution is not the primary acceptance gate. The primary gate is typed text/style/structure plus protected package evidence.
- Existing typed-mode regression tests remain mandatory. This PRD adds clean-sync tests; it does not replace no-op, package, opaque-node, baseline, normalization, or independent-verify coverage.

## Out of Scope

- An interactive CodeMirror, VS Code, Obsidian, browser, or desktop editor with caret rendering, undo history, selection UI, or formatting toolbar.
- Perfect emulation of undocumented Word internals or every version/platform-specific caret edge case.
- Keep Source Formatting paste, arbitrary rich clipboard payloads, or transfer of styles from an external document.
- Creating, deleting, renaming, or editing Word styles, relationships, hyperlinks, comments, bookmarks, fields, drawings, revisions, or opaque XML through clean text editing.
- Automatic three-way merge between a stale `edit.md`, a changed DOCX source, and a changed typed source.
- Editing nested table paragraphs, text boxes, headers, footers, footnotes, endnotes, or other containers outside the existing editable surface.
- Inferring semantic intent from a mixed-style full-paragraph rewrite with no unchanged anchor. It fails closed by default; an explicit future policy would require a separate ADR.
- Automatic style redistribution based only on visible-text similarity when equally valid alignments produce different ownership.
- Arbitrary manual style annotations in `edit.md`. Exact style ownership remains available through raw typed mode until a separate governed format-editing feature is designed.
- Guaranteeing identical line wrapping, pagination, page count, or rendered pixels after text length changes.
- Guaranteeing simultaneous atomic replacement of two independent flat files across process termination. The stale/missing projection recovery contract is used instead.
- Legacy run-numbered workdir migration without the original DOCX.

## Further Notes

The design separates evidence from inference:

| Rule | Status |
| --- | --- |
| Unformatted/merge-formatting paste uses the destination paragraph and direct formatting immediately preceding the cursor | documented by Microsoft Support |
| Typing replaces a selected range | documented by Microsoft Support |
| Paragraph-mark character properties can exist separately from ordinary run properties | documented by Open XML / Microsoft Learn |
| Start-of-paragraph insertion uses the right visible unit | project-defined deterministic policy |
| Non-start insertion uses the left visible unit, including a formatted space | project policy informed by documented paste behavior and fixtures |
| Mixed local replacement uses selection-start style only under the anchored exception | project-defined safety tie-break |
| Ambiguous ownership fails closed | project-defined safety rule |

Microsoft Support says Word's **Merge Formatting** and **Keep Text Only** options use the destination paragraph's style and the direct formatting or character-style properties of text immediately preceding the cursor. This supports a left-context insertion rule, but it does not define every `Selection.TypeText` case or prove that mixed selections use the first character's style.

Microsoft Support separately documents the basic editing model: place the cursor and type to add text, select text and type to replace it, and apply formatting to a selected range. Microsoft Learn's WordprocessingML documentation models a paragraph as `w:p` containing runs `w:r`, with paragraph properties in `w:pPr` and character properties in run properties. `ParagraphMarkRunProperties` documents the paragraph mark's `w:rPr`, which is why `insertion_style_id` cannot be inferred only from visible-text majority style.

Unicode UAX #29 defines extended grapheme cluster boundaries and requires an implementation to choose the default UAX29-C1-1 rules or a precisely specified profile. This project pins the clean-sync segmentation contract to UAX29-C1-1 with the repository's Unicode 16.0.0 catalog. Protected tokens remain separate atomic units. No diff may split a grapheme cluster or protected token.

The project must capture a Word reference matrix for the six observable caret cases and paste modes. The matrix is evidence for policy maintenance, not a license to add caller-specific exceptions. If the reference matrix disproves a rule, update this policy, its fixtures, and ADR 0036 together.

Source links:

- https://support.microsoft.com/en-us/office/control-the-formatting-when-you-paste-text-20156a41-520e-48a6-8680-fb9ce15bf3d6
- https://support.microsoft.com/en-us/word/training/add-and-edit-text
- https://learn.microsoft.com/en-us/office/open-xml/word/how-to-apply-a-style-to-a-paragraph-in-a-word-processing-document
- https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.paragraphmarkrunproperties?view=openxml-3.0.1
- https://learn.microsoft.com/en-us/office/vba/api/word/selection.typetext
- https://unicode.org/reports/tr29/
- https://learn.microsoft.com/en-us/archive/blogs/murrays/richedit-character-formatting
- https://learn.microsoft.com/en-us/answers/questions/5067864/microsoft-word-bug-when-pasting-after-ctrl-b
- https://kadansky.com/files/newsletters/2025/2025_06_30.html

The hard problem is not adding another markup language. It is establishing one safe seam where plain-text edits are converted into existing typed structure with explainable ownership, while preserving a canonical source and a recoverable projection. `edit.md` is the Agent surface; `typed.md` is the structured source; `sync` is the governed conversion; `build` never guesses which one is authoritative.

## P0 Contract Amendment: authoritative edit binding

The `edit.md` header is a visible, human-readable mirror only. It is not an
authoritative source of freshness state because it is on the Agent editing
surface. The authoritative binding is a generated, CLI-managed
`edit.state.json` sidecar outside that surface:

- `schema`: `typed-clean-edit-state-1`
- `edit_schema_version`
- `sync_contract_version`
- `segmentation_contract`
- `base_typed_sha256`
- `base_projection_sha256`

Every `status`, `refresh`, `sync`, `validate`, and `build` operation loads and
validates the sidecar first. It computes freshness from the sidecar bindings,
never from hash values copied only in `edit.md`. It also compares the
`edit.md` header bindings with the sidecar. Any mismatch fails closed with
`edit-binding-mismatch` (or the more specific `edit-header-tampered`
diagnostic) before state classification or DOCX publication.

The state table's `dirty` action is limited to `edit status`, projection
grammar inspection, and `edit sync`. Top-level `validate` and `build` always
reject `dirty`, `stale-clean`, and `conflict`.

Successful `extract`, `refresh`, and `sync` operations must publish the
projection, authoritative sidecar, and run evidence as one success condition.
If evidence or sidecar publication fails, the command fails and must not
report success. A failed operation leaves the previous canonical artifacts
unchanged; an interrupted later replacement is reported as stale or missing.

For an existing valid typed workdir that predates the projection, the explicit
upgrade entry point is `edit refresh --init`. It may create the derived
`edit.md`, `edit.state.json`, and initialization evidence only after the
existing typed workdir passes its current validation. This is projection
initialization, not a DOCX or typed-schema migration.
