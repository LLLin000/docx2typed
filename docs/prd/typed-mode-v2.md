# PRD: Typed mode v2 — format-safe DOCX editing

## Problem Statement

The current docx2typed workflow exposes each Word run as a numbered Markdown line. That preserves a replay skeleton, but it makes ordinary language editing difficult: one sentence can be split across many implementation-level run boundaries, and an AI must preserve line counts and numbering while rewriting text. The format is faithful to Word's internal slicing rather than to the user's editing task.

The current implementation also has package-integrity risks for a new typed workflow. A broad document serializer can rewrite XML outside the intended edit region, a flat paragraph scan can confuse table paragraphs with direct body paragraphs, and paragraph-level XML comparison alone does not prove that a typed source was converted to the intended DOCX. Manual edits to the source DOCX after extraction can also leave the editing workdir based on a stale template.

Superscript and subscript introduce a second ambiguity. A Unicode character such as U+2082 and an ordinary character with Word `vertAlign=subscript` may look similar while representing different text and formatting. The default workflow must preserve that distinction, while an explicit, auditable normalization workflow may convert selected representations.

## Solution

Make typed mode the only format on the experiment branch. A source DOCX is extracted into a self-contained typed workdir containing a restricted Markdown-like source, XML/manifest sidecar data, a character-style registry, and an immutable template copy. The source is edited only through the typed text file; formatting structure is locked.

The typed source is parsed by a project-owned restricted grammar into one shared flat AST. Text nodes carry effective style IDs; anchors, inline tokens, range containers, and opaque nodes remain structural nodes. Clean/style/raw views, validation, building, normalization, and independent verification all consume this AST.

Untouched direct-body paragraphs are copied from the template at the original byte level. Touched paragraphs are generated from the validated AST while retaining the original paragraph properties and approved structural nodes. A package guard verifies that all non-editable DOCX parts and protected XML regions remain unchanged. Output is written transactionally and is published only after independent verification succeeds.

The normal workflow preserves Unicode and Word-style superscript/subscript representations. An optional normalization workflow first reports candidates, lets the agent make occurrence-level decisions, applies versioned Unicode transformation recipes, writes an audit record, emits a new normalized DOCX baseline, and re-extracts a new workdir.

## User Stories

1. As an AI editor, I want to extract a DOCX into one typed workdir, so that all source, XML, style, and template inputs remain paired and verifiable.
2. As an AI editor, I want paragraphs represented as continuous natural-language text, so that I can rewrite prose without maintaining Word run numbers.
3. As an AI editor, I want ordinary text to remain literal except for approved typed tags, so that Markdown rendering rules cannot silently change patent or scientific text.
4. As an AI editor, I want clean view to hide typed markup, so that I can read the document as continuous prose.
5. As an AI editor, I want style view to show style boundaries and labels, so that I can understand formatting without inspecting raw XML.
6. As an AI editor, I want raw view to expose the exact typed source, so that I can diagnose grammar, IDs, and structural tokens.
7. As an AI editor, I want entity escaping for literal ampersands and angle brackets, so that ordinary text cannot be mistaken for typed structure.
8. As an AI editor, I want paragraph text stored on one logical source line, so that file newlines cannot be confused with Word line breaks.
9. As an AI editor, I want explicit line-break and tab tokens, so that non-text inline content is preserved without invisible characters.
10. As an AI editor, I want style spans tied to stable content-addressed style IDs, so that paragraph order changes do not rename unrelated styles.
11. As an AI editor, I want the style registry to expose diagnostic features such as bold, font, and vertical alignment, so that style meaning is visible without editing XML.
12. As an AI editor, I want styles and structural tokens locked in the normal workflow, so that text edits cannot silently change document formatting.
13. As an AI editor, I want text edits inside an existing style span to retain that style, so that replacing a word does not remove its formatting.
14. As an AI editor, I want empty spans removed and adjacent equivalent spans merged in memory, so that normal text deletion does not create invalid markup.
15. As an AI editor, I want cross-span rewrites rejected when style ownership is ambiguous, so that the builder never guesses a formatting assignment.
16. As an AI editor, I want inserted paragraphs to declare an existing paragraph to inherit from, so that numbering, indentation, alignment, and base style are explicit.
17. As an AI editor, I want paragraph deletion represented by an explicit tombstone, so that an accidental block deletion cannot silently remove document content.
18. As an AI editor, I want paragraph IDs to be unique lookup keys while source order controls output order, so that insertion and deletion do not corrupt sidecar matching.
19. As an AI editor, I want existing hyperlink text to be editable while its target remains locked, so that wording can change without changing document relationships.
20. As an AI editor, I want existing comment and bookmark ranges to remain paired and immovable while their ordinary text can change, so that annotations survive text editing.
21. As an AI editor, I want unsupported fields, revisions, drawings, and other complex nodes shown as opaque diagnostics, so that I know content is present without being invited to edit it unsafely.
22. As an AI editor, I want a paragraph containing an opaque node to reject touched builds, so that unsupported XML is never regenerated from an incomplete model.
23. As an AI editor, I want tables, text boxes, headers, footers, and other nested containers preserved outside the editable surface, so that body editing cannot flatten or duplicate their paragraphs.
24. As an AI editor, I want the builder to reject a template whose fingerprint changed, so that manual DOCX edits cannot be overwritten by a stale workdir.
25. As an AI editor, I want source drift detected when the original source is still available, so that I know when to create a new workdir.
26. As an AI editor, I want build to fail before writing output when grammar, structure, template, or package validation fails, so that no invalid DOCX is published.
27. As an AI editor, I want untouched paragraphs copied from the original XML, so that their formatting and non-text XML remain byte-stable.
28. As an AI editor, I want touched paragraphs generated only from approved style and structure data, so that formatting changes are constrained by the typed contract.
29. As an AI editor, I want all non-editable DOCX package parts checked by content hash, so that changes to styles, numbering, relationships, headers, footers, media, or comments are detected.
30. As an AI editor, I want output written transactionally, so that an interrupted build cannot replace a valid prior output with a partial document.
31. As an AI editor, I want independent verify to reconstruct the baseline and compare the output with the typed source, so that a bug shared by build and validation cannot pass unnoticed.
32. As an AI editor, I want layout reflow distinguished from template-format damage, so that changed line wrapping or pagination is reported honestly rather than treated as an XML failure.
33. As an AI editor, I want Unicode superscript/subscript characters preserved by default, so that text code points and Word formatting semantics are not silently conflated.
34. As an AI editor, I want style view and candidate reports to distinguish Unicode vertical characters from Word `vertAlign`, so that I can make a semantic decision.
35. As an AI editor, I want a versioned Unicode catalog covering digits, signs, operators, delimiters, letters, modifiers, ordinals, combining marks, and exceptional characters, so that candidate discovery is complete and auditable.
36. As an AI editor, I want every catalog candidate classified as approved, ambiguous, manual, or unsupported, so that an unclassified character cannot disappear from the normalization workflow.
37. As an AI editor, I want approved candidates convertible in an `all` normalization profile, so that safe bulk normalization is possible.
38. As an AI editor, I want ambiguous and manual candidates selectable individually, so that identical code points can be handled differently in different semantic contexts.
39. As an AI editor, I want normalization policies to include the source fingerprint, catalog hash, and occurrence decisions, so that a policy cannot be applied to the wrong workdir.
40. As an AI editor, I want normalization to compose vertical style with existing bold, font, color, and language properties, so that conversion does not strip surrounding formatting.
41. As an AI editor, I want multi-character and non-reversible transformations recorded explicitly, so that Unicode compatibility mappings are not mistaken for lossless replacements.
42. As an AI editor, I want normalization to require a decision for every required candidate, so that incomplete semantic review cannot produce a mixed undocumented result.
43. As an AI editor, I want normalization to create a new workdir and leave the original untouched, so that both baselines remain reproducible.
44. As an AI editor, I want normalization to be idempotent under the same policy, so that rerunning it does not create further changes.
45. As a maintainer, I want the parser, validator, builder, views, normalizer, and verifier to share one AST, so that fixes to grammar and structure cannot diverge across commands.
46. As a maintainer, I want schema and canonicalizer versions recorded with each workdir, so that incompatible future rules fail closed instead of guessing.
47. As a maintainer, I want the default CLI to operate on a workdir rather than scattered sidecars, so that users cannot accidentally combine files from different documents.
48. As a maintainer, I want real Word-generated DOCX fixtures covering supported and unsupported structures, so that tests exercise the XML shapes that cause production failures.
49. As a maintainer, I want no-op extract/build/verify tests, so that any future change that damages untouched template content fails immediately.
50. As a maintainer, I want forbidden mutations tested as pre-output failures, so that the test suite proves the safety boundary rather than merely checking that Word opens the result.

## Implementation Decisions

- **Clean cutover:** typed mode is the sole format on the experiment branch. Existing run-numbered workdirs are not migrated from Markdown; the original DOCX is required for re-extraction.
- **Typed workdir:** extraction produces one paired project containing typed source, XML/manifest sidecar data, style registry, and immutable template copy. Build, validation, views, and verification accept the workdir as their unit of input.
- **Restricted grammar:** typed source is Markdown-like but not CommonMark or a generic HTML document. A project-owned parser recognizes paragraph directives, deletion tombstones, style spans, anchors, inline tokens, range containers, and opaque tokens.
- **Text representation:** ordinary text uses XML entity escaping for ampersand and angle brackets; Unicode code points and whitespace are preserved; one paragraph uses one logical source line; real Word breaks use explicit tokens.
- **Flat AST:** effective character formatting lives on Text nodes as style IDs. Span tags are only a serialization projection. Structural nodes represent anchors, tabs/br, hyperlinks, and opaque references.
- **Editable surface:** only direct body paragraphs are typed-editable. Tables, text boxes, headers, footers, footnotes, and other nested containers remain opaque template content. A paragraph with an opaque node may replay unchanged but cannot enter the touched synthesis path.
- **Paragraph identity:** IDs are unique sidecar lookup keys, not contiguous line numbers. Source order controls output order. New paragraphs require explicit inheritance from an existing paragraph; deleted paragraphs require explicit tombstones.
- **Base style:** each paragraph base style is selected by visible-character-weighted majority, with first occurrence breaking ties. It is the fallback style for unspanned new text, not Word's full style-inheritance default.
- **Style registry:** character styles use content-addressed IDs derived from conservative canonical rPr. The raw rPr remains the generation source; canonicalization is used for identity, deduplication, merging, and comparison. The registry is immutable during normal text-only editing.
- **Structure skeleton:** existing style IDs, structural token order, range attributes, anchor IDs, and opaque references are locked. Empty-span removal and adjacent equivalent-style merging are allowed as text-driven in-memory normalization.
- **Relations and annotations:** existing hyperlink text may change while relationship IDs and targets remain fixed. Existing comment/bookmark ranges may contain changed text while their IDs, names, pairing, and order remain fixed. New or deleted relationships and annotations are out of scope.
- **Baseline and drift:** the baseline is re-derived in memory from the fingerprint-matched template. Source/template drift blocks build; the system does not perform an automatic three-way merge of pending typed edits.
- **Byte-preserving build:** the builder locates direct-body paragraph byte ranges in the original document XML using an XML-aware tokenizer. Untouched ranges and all protected XML bytes are copied; only approved touched ranges are generated.
- **Package guard:** the sidecar records source/template fingerprints and content hashes for all package parts. Build and verify require all non-editable parts and protected document XML regions to remain unchanged.
- **Transactional output:** build writes a temporary output, runs validation and independent verification, and atomically publishes the final DOCX only after every check passes.
- **Independent verify:** verification re-derives the baseline, parses the typed source, parses the output DOCX, compares text and structure, and checks package integrity independently of build's intermediate state.
- **Views:** clean, style, and raw are deterministic read-only projections produced from the shared AST. No interactive editor is included in this PRD.
- **No mutation API in v2.0:** typed.md is the only editing surface. A visible-text mutation API may be added later from observed failure cases, but it is not a second source of editing semantics in this scope.
- **Vertical normalization:** default extraction preserves Unicode and Word-style vertical representations. Optional normalization uses a pinned Unicode catalog, agent-visible occurrence candidates, explicit policies, style-delta composition, audit records, and a new normalized baseline.
- **Catalog policy:** the catalog is generated from a pinned Unicode data version and committed with its hash. Every candidate is classified as approved, ambiguous, manual, or unsupported. `all` converts approved entries only; selective decisions are occurrence-level.
- **Normalization output:** normalization never mutates the source or current workdir. It emits a new normalized DOCX/workdir and requires a complete policy before committing it.
- **CLI seam:** the highest integration seam is workdir → build → output DOCX → independent verify. Lower-level parser, canonicalizer, locator, and patcher tests support failures that are difficult to diagnose at that seam.

## Testing Decisions

- Tests must defend observable behavior and invariants, not implementation details such as private helper calls or exact internal class layouts.
- The primary seam is the full typed workdir workflow: extraction baseline, typed edit, validation, build, package guard, and independent verify.
- A real DOCX fixture corpus is required because Word-generated namespace declarations, rsid attributes, relationships, empty runs, fields, section properties, and nested containers are not reliably represented by synthetic XML alone.
- Fixture coverage includes ordinary styles, adjacent equivalent runs, bold/font/color plus vertical alignment, Unicode vertical characters, signs and delimiters, whitespace, tabs, breaks, hyperlinks, comments, bookmarks, opaque fields/drawings/revisions, tables, numbering, multiple sections, insertion, deletion tombstones, source drift, and wrong-template inputs.
- No-op extract/build/verify is the baseline regression test. It must prove package-part integrity and protected XML preservation, not only that the output opens.
- Text-only edits test style preservation within an existing Text node, whitespace preservation, entity round-tripping, paragraph ordering, and independent output verification.
- Forbidden operations test that validation fails before final output publication: changed style IDs, new relationships, moved anchors, malformed ranges, unsupported touched nodes, missing deletion tombstones, invalid inheritance, incomplete normalization policies, and schema/catalog mismatches.
- Patcher tests verify direct-body paragraph selection and prove that nested table paragraphs are not flattened or duplicated.
- Package tests compare decompressed content hashes for protected package parts and protected document XML regions; whole ZIP byte equality is not required because archive metadata may differ.
- Normalization tests verify approved, ambiguous, manual, unsupported, multi-character, non-reversible, conflicting-style, occurrence-level, audit, and idempotence behavior.
- Independent verify must be exercised separately from build so that a shared builder/validator assumption cannot be the only proof of correctness.
- Rendering through Word or LibreOffice may be an additional smoke check, but it is not the primary acceptance gate because pagination, fonts, and layout reflow are environment-dependent.
- The current codebase has no visible automated fixture suite, so the PRD introduces the real-DOCX corpus rather than relying on prior test conventions.

## Out of Scope

- Compatibility with the legacy run-numbered format or automatic migration from old Markdown without the original DOCX.
- Generic CommonMark, HTML5, browser DOM, or third-party Markdown AST compatibility.
- Interactive CodeMirror, VS Code, Obsidian, or browser-based editing UI.
- Full hierarchical editing of tables, text boxes, headers, footers, footnotes, fields, revisions, drawings, OLE objects, or other nested containers.
- Creating, deleting, retargeting, or otherwise authoring hyperlinks, comments, bookmarks, fields, relationships, or Word styles during normal editing.
- Arbitrary format editing, raw pPr editing, direct rPr authoring, or style registry mutation in text-only mode.
- Automatic cross-span visible-text diffing or style redistribution.
- Automatic three-way merge between pending typed edits and a manually modified source DOCX.
- Guaranteeing identical line wrapping, pagination, page count, or rendered pixel output after text length changes.
- Full semantic resolution of Word's `docDefaults`, named styles, theme fonts, and inheritance graph.
- Runtime regeneration of the Unicode catalog from the host Python installation.
- Treating Unicode and Word-style superscript/subscript as equivalent in the default workflow.
- Automatic conversion of every visually similar Unicode character; ambiguous and manual candidates require explicit policy decisions.
- A complete structured mutation API such as `replace_paragraph`, `insert_paragraph`, or `delete_paragraph` beyond typed.md editing and validation.

## Further Notes

The current implementation is a run-replay prototype, not a safe foundation for typed mode. The typed builder must not rely on a whole-document serializer for its package-integrity contract, and the paragraph extractor must distinguish direct body paragraphs from nested table paragraphs.

The implementation should proceed in dependency order: restricted grammar and flat AST; workdir and sidecar schema; baseline extraction and direct-body XML locator; byte patcher and package guard; validator and independent verify; deterministic views; then the Unicode catalog, candidate report, normalization policy, and audit path.

`CONTEXT.md` is the project glossary and the recorded ADRs are the design decisions for this PRD. Any implementation that conflicts with them must reopen the relevant decision rather than silently adding a second convention.
