# Word caret/paste behavior reference matrix

Research note for issue #6 (wayfinder:research). Evaluates the six caret/paste rules the
clean-edit style policy claims to model (`docs/prd/typed-mode-word-editing.md` → "Word-like
style policy" / "Caret context" / "Empty-paragraph insertion style"; ADR 0036) against the
most authoritative published evidence obtainable on this machine.

**Measurement limitation (explicit):** this machine has LibreOffice
(`C:/Program Files/LibreOffice`) but **no Microsoft Word installation**. Live Word
measurement was impossible here; every claim below is grounded in published sources that
were fetched and read directly during this investigation (URLs + verbatim quotes), ranked
by authority in the section after the matrix. Rules whose Word behavior no published
primary source settles are marked **unverifiable-without-live-Word**, and a measurement
protocol for a human with Word is included at the end.

## Source authority ranking

| Tier | Source | What it establishes |
| --- | --- | --- |
| 1 — normative spec | ISO/IEC 29500 / ECMA-376 Part 1 §17.3.1.29 `rPr` (Run Properties for the Paragraph Mark), read via the c-rex OOXML reference mirror; MS Learn Open XML SDK `ParagraphMarkRunProperties` | Existence and XML location of paragraph-mark `w:rPr`; that the ¶ is a real character with run properties |
| 1 — official product docs | Microsoft Support: *Control the formatting when you paste text* (20156a41-520e-48a6-8680-fb9ce15bf3d6, ms.date 11/19/2025); Microsoft Support: *Add and edit text* (Word training); MS Learn: `Word.Selection.TypeText` (VBA); MS Learn: RichEdit `EM_REPLACESEL`; MS Learn: `Selection.CopyFormat` (Word PIA) | Paste formatting rules (left-context); typing replaces selection; TypeText semantics; RichEdit replacement-format rule (selection first character); Word's first-character convention for format copying |
| 2 — Microsoft Q&A / MVP corroboration | MS Answers thread 5067864 *Microsoft Word bug when pasting after Ctrl B* (answers by MVP Suzanne S. Barnhill and Alex Chen; observed Word 2013–365) | Unformatted paste takes the previous character's format; a formatted space makes pasted text formatted; paste ignores the Ctrl+B typing toggle; typing toggle affects what you type, not what you paste |
| 3 — long-standing independent measurement | Kadansky newsletter 2025_06_30 *Think You Know How Bold, Italic, etc. Actually Work? Take This Quiz!* (Word/Pages/LibreOffice, Windows+Mac) | Typing format by caret position: document start → right context; elsewhere → left context; after a space → the space's own (invisible) format; empty document → current/default formatting |
| 3 — Word MVP reference | Barnhill, *Default Paragraph Font Explained* (wordfaqs.ssbarnhill.com) | Paragraph style's defined character formatting is what applies when no direct formatting exists (the "base style" concept behind the fallback chain's last level) |
| 4 — repo-internal policy | PRD `typed-mode-word-editing.md` (Further Notes table + Word-like style policy); ADR 0036; `scripts/edit_sync.py:_assign_style`; `scripts/typed_docx.py:_paragraph_insertion_style` | What the project claims and what it implements; the PRD itself flags which rules are project-defined rather than Word-documented |

Caveat on the RichEdit sources: Word for Windows does **not** use the RichEdit control
(WordPad does). `EM_REPLACESEL` documents RichEdit, not Word's editor engine, and is
treated here as related-engine evidence only. Word's own typing-format rules are almost
entirely undocumented by Microsoft; the strongest Microsoft-adjacent documentation is the
paste article and the Q&A thread above.

## Reference matrix

Implementation mapping: `edit_sync.py:_assign_style` (caret rules, reasons
`paragraph-start-right-context`, `left-context`, `single-style-replacement`,
`mixed-selection-start-anchored`); `typed_docx.py:_paragraph_insertion_style` (fallback
chain, docstring: "paragraph-mark `w:rPr`, then the first text run's style, then the base
style").

| # | Rule | Claimed policy (PRD / code) | Evidence found (sources + verbatim quotes) | Verdict | Recommended action |
| --- | --- | --- | --- | --- | --- |
| 1 | Paragraph-start insertion (offset 0) | "Pure insertion at paragraph offset 0 with visible text to the right → Style of the first visible unit to the right." PRD Further Notes: "Start-of-paragraph insertion uses the right visible unit — **project-defined deterministic policy**." Code: `i1 == 0` → `right.style`, reason `paragraph-start-right-context` | Kadansky quiz #1 (`\|football`): "**a. bold**: You're typing at the start of the document, with a bold character to the right" and rule "If you type at the very beginning of the document, your new text will arrive with the same formatting as the very first character in the document". **Documents document-start only.** No Microsoft or MVP source found that states what Word does at the start of a *non-first* paragraph (left context there is the previous ¶, whose `w:rPr` may differ from the first visible character). The PRD itself does not claim Word documents this. | **unverifiable-without-live-Word** for non-first paragraphs; documented (right-context) only for document start | Keep as deterministic project policy (it is already labeled as such in the PRD — no doc correction needed). Add the non-first-paragraph case to the measurement protocol; if live Word shows left-context/¶ behavior at paragraph start, update PRD policy table, its fixtures, and ADR 0036 together (per PRD's own maintenance rule) |
| 2 | Non-start insertion (left context, incl. formatted space) | "Pure insertion at any other caret position → Style of the visible unit immediately to the left, including a formatted space." Code: `left.style`, reason `left-context`. "The synchronizer never trims a space before choosing context" | MS Support paste article: "The text also takes on any direct formatting or character style properties of text that **immediately precedes the cursor** when the text is pasted" (Merge Formatting; identical wording under Keep Text Only). Kadansky: "If you type anywhere else in the document, your new text will use the formatting of the character **immediately to the left** of where you're typing" and quiz #5/#6: after a space, "**c. cannot be determined**: …with a space to the left, so you can't tell simply by looking at it whether that space is bold or plain". MS Answers 5067864 (Alex Chen): "when you paste the non-formatted text in Word, it's going to apply the format of the previous character… If I press CTRL+B and press space, as the space's format is bold, the pasted content will be changed to bold as well." | **holds** (two independent lines of published evidence agree: paste left-context per Microsoft; typing left-context per kadansky + formatted-space per MS Answers) | No policy change. Optionally record in the PRD that the formatted-space behavior now has direct Q&A evidence, not only the paste article |
| 3 | Empty-paragraph typing (paragraph-mark `w:rPr` / `insertion_style_id` fallback chain) | "Pure insertion into an empty paragraph → `insertion_style_id`"; extraction = paragraph-mark `w:rPr` → first text run's style → paragraph base style ("Extraction records the paragraph-mark `w:rPr` when present; otherwise it follows the validated project fallback for the empty-run typing context, then the paragraph base style") | ISO 29500 §17.3.1.29 (c-rex mirror): "This element specifies the set of run properties applied to the glyph used to represent the physical location of the paragraph mark for this paragraph. This paragraph mark, being a physical character in the document, can be formatted… If this element is not present, the paragraph mark is unformatted, as with any other run of text." Open XML SDK `ParagraphMarkRunProperties`: "Run Properties for the Paragraph Mark. This class is available in Office 2007 and above… qualified name is w:rPr" (children include `RunStyle`, `Bold`, `FontSize`, `Color`, …). Kadansky (empty-document): "If you type into an empty document, your new text will use the current or default formatting". Barnhill DPF: style-defined font applies "when no direct font formatting or character style has been applied" (base-style level concept). MS Answers 5067864 (typing toggle writes the ¶'s format: Ctrl+B then space → bold space → bold paste) corroborates that Word's stored empty-context formatting is what new text/paste consumes | **holds-with-caveat**: the storage claim (`w:rPr` on the ¶) is fully documented; that Word *uses* that stored format for typing into an empty paragraph is strongly corroborated but not Microsoft-documented; the **priority order** (rPr → first run → base) is project-defined and fixture-tested, not observable in a truly empty paragraph beyond level 1 and 3 | Keep the chain; it is documented as a project fallback already. Ensure fixture corpus includes paragraphs with explicit ¶ `w:rPr` differing from visible-text style (Slice C fixture already lists "paragraph-mark formatting"). No PRD text change needed |
| 4 | End-of-paragraph insertion | "Pure insertion at the end of a paragraph → final visible text unit's style" (user story 7; in code this is the ordinary `left-context` branch — no separate end case) | No source documents any end-of-paragraph exception. Kadansky's "anywhere else in the document… character immediately to the left" covers the end of a paragraph (the final character is the left context). MS Support paste article's "immediately precedes the cursor" likewise applies at paragraph end. Selection behavior of the ¶ itself (selecting ¶ + typing replaces the paragraph mark) is a separate, out-of-model case (project treats ¶ as non-visible structure) | **holds** (plain special case of rule 2; no conflicting evidence) | No change. Note in the PRD (optional) that end insertion is not a distinct Word rule but the left-context rule at the paragraph boundary |
| 5 | Single-style replacement | "Replacement wholly inside one effective style → That selected range's style." Code: `styles = {unit.style …}; if len(styles) == 1: return styles.pop(), "single-style-replacement"` | MS Support *Add and edit text*: "Select the text you want to replace… Start typing" (selection-replacement model, no format rule stated). RichEdit `EM_REPLACESEL` (related engine): "In a rich edit control, the replacement text takes the formatting of the character at the caret **or, if there is a selection, of the first character in the selection**." For a uniform-style range, every plausible rule (first char / last char / range style) yields the same result, so the policy is insensitive to the unsettled Word detail | **holds** (trivially: any documented selection-format rule agrees on a single-style range; basic replacement model documented) | No change |
| 6 | Local mixed-style replacement (selection-start tie-break) | "Local mixed-style replacement with an unchanged anchor on at least one side… → Style at selection start; emit a warning." Code: `start = next((unit for unit in base if not unit.token), base[0]); return start.style, "mixed-selection-start-anchored", …`. PRD Further Notes: "Mixed local replacement uses selection-start style only under the anchored exception — **project-defined safety tie-break**" | RichEdit `EM_REPLACESEL`: "the replacement text takes the formatting of… the first character in the selection" — matches selection-start, but for RichEdit, not Word's engine. Word PIA `Selection.CopyFormat` (Format Painter, a different operation): "copies the character formatting of the first character in the selected text" — shows Word's "first character of selection" convention exists for format-copy operations, but no Microsoft source extends it to typing. MS Answers 5067864 confirms only paste/type left-context, nothing about mixed selections | **unverifiable-without-live-Word** (related-engine + adjacent-operation evidence is consistent with the tie-break; Word's own engine behavior for typing over a mixed selection is undocumented) | Keep the tie-break as a deterministic, warned, project-defined rule (PRD already labels it as such). Add to measurement protocol; if live Word differs, only the PRD's "Further Notes" phrasing needs a caveat — the safety behavior (warn + evidence, fail closed without anchor) is policy, not a Word claim |

### Annex row — paste modes (context for rules 1–4)

| Paste mode | Claimed policy | Evidence | Verdict |
| --- | --- | --- | --- |
| Unformatted / Merge Formatting paste | Modeled by the same destination-context policy as typing ("unformatted pasted content represented by the same destination-context policy as clean text edits"; "It does not reproduce Keep Source Formatting paste") | MS Support: Merge Formatting "discards most formatting that was applied directly to the copied text… The text takes on the style characteristics of the paragraph where it is pasted. The text also takes on any direct formatting or character style properties of text that immediately precedes the cursor"; Keep Text Only: identical left-context sentence. MS Answers 5067864: paste of plain text "applies the format of the previous character" while **ignoring the Ctrl+B typing toggle** (Barnhill: "After pressing Ctrl+B, what you next type will be bold, but pasted text will not change its format") — i.e., Word's paste context is the destination characters, exactly the project's model. Keep Source Formatting retains source formatting (out of model by design) | **holds** for the two modeled modes; Keep Source Formatting correctly excluded |

## Measurement protocol (for a human with Word — settles the two unverifiable rows)

General preparation, for every scenario:

- Use Word for Microsoft 365 (or 2016/2019/2021) on Windows; run each scenario twice on a
  fresh document. Turn **off** "Smart cut and paste" (File → Options → Advanced → Cut,
  copy, and paste → Settings → uncheck spacing adjustments) so auto-spacing does not
  confound results.
- Show formatting marks (Home → ¶ / Ctrl+Shift+8) so the ¶ and spaces are visible.
- Record results two ways: (a) visually (bold/plain on screen; the Font group's B button
  state while the caret sits at the insertion point — kadansky's toolbar check), and
  (b) structurally: save the document, open `word/document.xml` from the `.docx` (rename
  to `.zip` and extract), and inspect which `<w:r>` the typed text landed in and which
  `<w:rPr>` that run carries. Structural inspection is the ground truth; screen state can
  lag or be ambiguous.
- Alternative programmatic capture: a VBA macro using `Selection.TypeText` then reading
  `Selection.Font.Bold` / `Selection.Font.Name` etc. — but note `Selection.TypeText` is a
  documented API (see VBA reference) and may differ from interactive typing in edge cases;
  treat interactive typing as primary.

Scenario A — paragraph-start insertion (rule 1):

1. Create two paragraphs: P1 = "football" with **"foot" bold** + "ball" plain; P2 = a
   second paragraph whose first character is bold and rest plain (e.g. also "football").
2. Caret at the very start of P1 (document start), type "X". Observe: bold or plain?
   (kadansky predicts bold — right context.)
3. Caret at the very start of P2 (non-first paragraph), type "X". Observe: bold (right
   context — policy prediction) or plain (left context = previous ¶, or the ¶ of P2 if
   that is Word's anchor)?
4. Variant: format the ¶ at the end of P1 as bold (select ¶, Ctrl+B). Repeat step 3.
   If X now comes out bold when the ¶ is bold but the first char of P2 is plain, Word is
   using ¶-anchored left context at paragraph start and the policy rule 1 would need
   correction (this is the decisive experiment).
5. Decides: right-context at every paragraph start → holds for all paragraphs;
   otherwise → needs-correction (restrict policy to document start or switch to ¶-left
   context, update fixtures + ADR 0036).

Scenario B — mixed-selection replacement (rule 6):

1. One paragraph: "plain **bold bold** plain" (selection spans the bold region plus one
   plain char on each side: "n **bold bo** pl").
2. Select that mixed range, type "NEW". Inspect XML: is "NEW" one run with the style of
   the first selected character (plain — policy prediction), the last character, or
   split? Also check the Font B button state during/after typing.
3. Variant: reverse selection direction (select from the right end leftward) — if the
   result changes, Word is direction-sensitive; note this for the tie-break documentation.
4. Decides: first-character style → holds; anything else → needs-correction (document the
   real rule, keep or adjust the tie-break; the anchored-warning behavior stays policy).

Scenario C — empty-paragraph typing (rule 3, priority-order confirmation):

1. Empty paragraph with a bold ¶ (type Ctrl+B into an empty paragraph; ¶ shows bold).
   Type "X": expect bold (X in a run whose rPr matches the ¶'s `w:rPr`).
2. Empty paragraph with plain ¶ inside a paragraph style whose style `rPr` is bold (e.g.,
   a Heading style): type "X". Expect the style's formatting (base-style level) — confirms
   level 3 of the chain.
3. Document both in XML (`<w:pPr><w:rPr>…` present vs. absent; `<w:rPr>` of the new run).
4. Decides: level 1 (¶ rPr) and level 3 (style rPr) confirmed; level 2 ("first text run's
   style" fallback) is unreachable in a truly empty paragraph by construction and remains a
   project-defined convenience for paragraphs that project as empty — no Word measurement
   can settle it; keep it labeled project-defined.

Cross-check (optional, non-authoritative): repeat scenarios A–C in LibreOffice Writer on
this machine and record results in a table row labeled "LibreOffice (NOT Word)" — useful
for spotting engine-family conventions but never a substitute for the Word measurement;
per the PRD, LibreOffice is a smoke check, not the acceptance gate.

## Verdict summary

- holds: 3 (non-start left-context incl. formatted space; end-of-paragraph insertion;
  single-style replacement)
- holds-with-caveat: 1 (empty-paragraph `w:rPr` storage documented; fallback priority
  order project-defined)
- unverifiable-without-live-Word: 2 (paragraph-start right-context for non-first
  paragraphs; mixed-selection selection-start tie-break)
- needs-correction: 0 — no published evidence contradicts any policy rule; both
  unverifiable rules are already labeled project-defined in the PRD, so no PRD/ADR/fixture
  change is forced. Re-run Scenarios A and B against live Word before any policy revision.

## Sources (all fetched directly during this investigation)

1. Microsoft Support — Control the formatting when you paste text:
   https://support.microsoft.com/en-us/office/control-the-formatting-when-you-paste-text-20156a41-520e-48a6-8680-fb9ce15bf3d6 (ms.date 11/19/2025)
2. Microsoft Support — Add and edit text (Word training):
   https://support.microsoft.com/en-us/word/training/add-and-edit-text
3. MS Learn — ParagraphMarkRunProperties Class:
   https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.wordprocessing.paragraphmarkrunproperties?view=openxml-3.0.1
4. MS Learn — Selection.TypeText method (Word):
   https://learn.microsoft.com/en-us/office/vba/api/word.selection.typetext
5. MS Learn — EM_REPLACESEL message (RichEdit):
   https://learn.microsoft.com/en-us/windows/win32/controls/em-replacesel
6. MS Learn — Selection.CopyFormat (Word PIA):
   https://learn.microsoft.com/en-us/dotnet/api/microsoft.office.interop.word.selection.copyformat?view=word-pia
7. MS Learn / Microsoft Q&A — "Microsoft Word bug when pasting after Ctrl B" (thread
   5067864, answers by Suzanne S. Barnhill MVP and Alex Chen):
   https://learn.microsoft.com/en-us/answers/questions/5067864/microsoft-word-bug-when-pasting-after-ctrl-b
8. Kadansky Consulting newsletter 2025_06_30 — "Word Processing: Think You Know How Bold,
   Italic, etc. Actually Work? Take This Quiz!":
   https://kadansky.com/files/newsletters/2025/2025_06_30.html
9. ISO/IEC 29500 / ECMA-376 Part 1 §17.3.1.29 — rPr (Run Properties for the Paragraph
   Mark), c-rex OOXML reference mirror:
   https://c-rex.net/samples/ooxml/e1/part4/OOXML_P4_DOCX_rPr_topic_ID0EIEKM.html
10. S. S. Barnhill — Default Paragraph Font Explained (WordFAQs):
    http://wordfaqs.ssbarnhill.com/DefParaFont.htm
11. Repo-internal: PRD docs/prd/typed-mode-word-editing.md; ADR docs/adr/0036-hash-bound-clean-edit-projection.md;
    scripts/edit_sync.py (`_assign_style`, ~lines 289–345); scripts/typed_docx.py
    (`_paragraph_insertion_style`, lines 445–468)
