# Ambiguous alignment enumeration — wayfinder research for issue #10

**Ticket:** https://github.com/LLLin000/docx2typed-typed-mode/issues/10
**Date:** 2026-08-06
**Status:** findings + recommendation (research only; no code changed)

## Verdict

The adjacency heuristic (`left.value == right.value and left.style != right.style`)
is **not** equivalent to the minimum-cost alignment enumeration promised by the
PRD ("Diff mapping", `docs/prd/typed-mode-word-editing.md` line 139). Of 12
adversarial cases run against the real CLI, **5 are silently mis-resolved**
(repeated-text replacements, deletions, and interleaved equal text), 1 rejects
only by alignment luck, and 1 exposes a contradiction inside the PRD contract
prose itself. **Recommendation: implement the enumeration (option a)** — it is
a bounded DP over the per-paragraph unit lists, reuses the existing
`_assign_style` policy unchanged, and removes a whole family of silent
wrong-style assignments. If the team declines, option (b) requires an honest
contract downgrade (exact amendment text below).

## Method

- Primary source: `scripts/edit_sync.py` (`flatten_paragraph`, `_assign_style`,
  `sync_paragraph`) in the main repo worktree; PRD "Diff mapping" decision and
  ADR 0036.
- Every case was run end-to-end against the real implementation: a real DOCX
  built with python-docx (runs with `bold`/`italic` direct formatting) →
  `python -m scripts extract` → edit.md body replaced with the draft →
  `python -m scripts edit sync`. Accept/reject, resulting `typed.md`, and the
  hunk evidence (`edit.state.json.run.json`) were recorded.
- Oracle: a full minimum-cost alignment enumerator (LCS DP over unit values,
  all-backtracking, dedupe) that applies the **same** `_assign_style` policy to
  every candidate alignment and compares per-draft-unit ownership. Two cost
  models were computed: **LCS** (cost = unmatched-unit count, i.e.
  SequenceMatcher semantics, replace = del+ins) and **LEV** (Levenshtein with
  replace cost 1). Verdicts are `unambiguous`, `ambiguous` (candidates
  diverge), or `all-reject`.
- Repro harness: `C:\Users\Lin\AppData\Local\Temp\ambig_check\harness.py`
  (temp dir; not committed). All 12 cases + SequenceMatcher opcodes
  reproducible from it.

## The contract vs the implementation

PRD line 139 ("Diff mapping"):

> "The synchronizer enumerates the minimum-cost alignment candidates needed to
> determine whether ownership is unique; if candidate alignments differ in
> style ownership, protected-token mapping, or changed ranges, it fails with
> `ambiguous-alignment`. If all candidates are ownership-equivalent, a
> documented deterministic tie-break may choose one and records
> `alignment-tie-resolved`."

ADR 0036: "Mixed-style replacement is rejected by default and is accepted only
for an anchored, uniquely aligned local hunk".

Implementation (`scripts/edit_sync.py`): `sync_paragraph` computes **one**
`SequenceMatcher` alignment (line ~415) and feeds each hunk to `_assign_style`
(lines 286–347), which contains the only ambiguity probes:

1. Insertion path (lines ~313–316): fires only when the **chosen** alignment
   places the insertion between `left.value == right.value` with different
   styles.
2. Mixed-replacement path (lines ~336–339): same check, only after the
   single-style and anchoring checks.
3. **Single-style replacement returns early (lines 314–321) and never looks
   at left/right context.** Pure deletion never assigns a style at all
   (`deletion-preserves-survivors`, line 439).

So the heuristic is reachable for insertions and mixed replacements but is
**structurally unreachable** for single-style replacements and deletions —
exactly the hunks that occur when repeated text loses one occurrence.

## Case matrix

Notation: `A(p)` = unit "A" plain, `A(b)` = bold, `A(i)` = italic. Draft text
shown with `X` = the touched text. "Caught" = CLI rejects or accepts in
agreement with the enumeration verdict; "MISS" = CLI accepts where the LCS
enumeration finds divergent ownership.

| # | Baseline units | Draft | CLI result (hunk evidence) | enum-LCS | enum-LEV | Verdict |
|---|---|---|---|---|---|---|
| c1 | `A(p) A(b) B(p)` | `A X A B` | REJECT `ambiguous-alignment` (insert between equal text) | all-reject | all-reject | caught — the ticket's example; unique optimal alignment, adjacency fires |
| c2 | `A(p) A(b) B(p)` | `A A X B` | ACCEPT `X`=bold (`left-context`) | unambiguous | unambiguous | control — the ticket's example; alignment unique, correctly accepted |
| c3 | `A(p) A(b)` | `A X` | ACCEPT `X`=bold (`single-style-replacement`, replaces the bold A) | **ambiguous** | unambiguous | **MISS** (LCS) — alternative optimal alignment replaces the *plain* A → `X` plain |
| c4 | `A(p) A(b) A(i)` | `A X A` | REJECT `ambiguous-alignment` (its alignment inserts X between plain/bold, deletes italic) | ambiguous | unambiguous | reject by luck — the rejection reason is alignment-luck, not enumeration; under LEV the unique answer is ACCEPT `X`=bold, so the CLI **over-rejects** |
| c5 | `A(p) B(p) A(b)` | `A X A` | ACCEPT `X`=plain | unambiguous | unambiguous | control — unique alignment (B has no twin) |
| c6 | `A(p) A(b)` | `A A X` | ACCEPT `X`=bold | unambiguous | unambiguous | control |
| c7 | `A(p) A(b)` | `A` | ACCEPT — deletes the **bold** A (`deletion-preserves-survivors`), survivor plain | **ambiguous** | **ambiguous** | **MISS** (both models) — optimal deletion also may delete the plain A, leaving a **bold** survivor |
| c8 | `A(p) B(b) C(i) D(p)` | `A X D` | ACCEPT `X`=bold + warning (anchored mixed) | unambiguous | unambiguous | control — unique alignment; the anchored mixed exception is sound when unique |
| c9 | `A(p) A(b) C(i)` | `A X C` | ACCEPT `X`=bold (replaces bold A) | **ambiguous** | unambiguous | **MISS** (LCS) — alternative replaces plain A → `X` plain |
| c10 | `A(p) A(b) B(p)` | `A B X A` | ACCEPT: deletes plain A, matches bold A to first draft A, inserts `X A` at end (`left-context`); typed `A(b) B X A(p)` | **ambiguous** | **ambiguous** | **MISS** (both models) — the ticket's interleaved-equal-text shape; alternative optimal matching inserts X *between* plain and bold A (rejected) or after plain A |
| c11 | `A(p) A(b)` | `X A` | ACCEPT: inserts X at paragraph start (right context = plain), deletes bold A; typed `X(p) A(p)` | **ambiguous** | unambiguous | **MISS** — LCS: `X` plain vs bold across candidates; LEV: unique optimum is replace plain A → `X` plain, **keep bold A**; the CLI's output (A plain) matches **no** optimal Levenshtein alignment |
| c12 | `A(p) B(b)` | `A X A B` | ACCEPT: `X` and the new `A` plain, `B` bold | unambiguous | unambiguous | contract-gap — ownership is identical across candidates but **hunk ranges differ** (X inserted before A vs after A); the PRD's "changed ranges" clause would fail this, its "ownership-equivalent tie-break" clause would accept |

**Missed-case count: 5 of 12 (c3, c7, c9, c10, c11) under the LCS cost model;
c7 and c10 remain ambiguous under Levenshtein too, and c11's output
contradicts the unique Levenshtein optimum. c4 additionally shows
verdict-by-luck: identical shape to c3/c9 but rejected instead of accepted,
purely because SequenceMatcher tie-breaking produced a different alignment.**
c12 shows the contract prose needs a pin regardless of which option is chosen.

## Why the heuristic misses (code-level)

- **Single-style replacement early return.** `_assign_style` lines 314–321:
  `if len(styles) == 1: return styles.pop(), "single-style-replacement", None`
  — the left/right adjacency probe is never reached. Every replacement of one
  occurrence of a repeated value (c3, c9, c11) takes this path, while an
  equal-cost alternative replaces the *other* occurrence with a different
  style.
- **Deletion assigns nothing.** `sync_paragraph`'s pure-deletion branch records
  the hunk and skips `_assign_style` entirely (c7). The choice of *which*
  occurrence is deleted changes the survivor's style.
- **Interleaved equal text.** With equal values on both sides of a gap, the
  alignment chooses which equal block the insertion/replacement neighbors;
  the adjacency probe only ever sees the **chosen** alignment's neighbors
  (c10: the chosen alignment puts X at the paragraph end where the probe is
  unreachable; an optimal alternative puts it between `A(p)` and `A(b)`).
- The two tickets' named examples (c1, c2) behave correctly: c1's unique
  optimal alignment lands between `A(p)`/`A(b)` so the probe fires; c2's
  alignment is genuinely unique. The heuristic covers exactly the case it was
  designed for — and nothing else.

## Cost-model finding (must be pinned either way)

The PRD says "minimum-cost" without defining the cost. The verdicts flip:

- Under **LCS/unmatched-unit count** (SequenceMatcher semantics; replace =
  del+ins — the model the implementation's own diff engine realizes): c3, c9,
  c11 are ambiguous.
- Under **Levenshtein replace=1**: c3, c9, c11 are unique (their CLI accepts
  are then correct); c7, c10 stay ambiguous; c11's CLI output is wrong even
  then; c4 becomes an over-rejection.

Recommend pinning the LCS model: it matches the implementation's diff engine,
keeps "replace" as one hunk (hunk identity / "changed ranges" is an LCS
concept), and a replace costing 1 would silently merge delete+insert into one
style decision, which is exactly the ambiguity the contract wants to surface.

## Recommendation

### (a) Implement enumeration (recommended)

Sketch, all inside `scripts/edit_sync.py` per paragraph:

1. Build the LCS DP table over `baseline_values` / `current_values`
   (O(n·m), n·m = unit counts; paragraphs are prose-scale).
2. Enumerate all optimal matchings by DP backtracking (follow every cell
   whose value equals an optimal predecessor), dedupe as frozensets of
   `(i, j)` pairs. Cap at a constant (e.g. 4096): on cap overflow, fail
   closed with `ambiguous-alignment` (repeated blocks that large are
   pathological, and failing closed is the contract's default).
3. For each matching, derive hunks (equal runs + gaps) and call the **existing
   `_assign_style`** on each gap — the policy code is reused unchanged. Record
   per-draft-unit ownership (style or reject reason).
4. Compare candidates pairwise: first divergence in ownership (or one
   accepting where another rejects) → `ambiguous-alignment`, message naming
   the two differing candidate ranges. All reject → first rejection reason
   (current behavior). All accept, identical ownership → keep the current
   SequenceMatcher alignment, and record `alignment-tie-resolved` in the hunk
   evidence (the PRD already promises this record).
5. Cheap skip: when the SequenceMatcher alignment contains no repeated value
   adjacent to any changed hunk, the matching is unique and enumeration
   reduces to today's path (zero overhead for ordinary prose).

Contract amendments to ship with (a):

- Pin the cost model in PRD line 139: "minimum-cost = fewest unmatched units
  (SequenceMatcher semantics; a replacement hunk costs its deleted plus
  inserted units)".
- Resolve the c12 contradiction: decide whether ownership-equivalent
  candidates with different changed ranges fail or tie-break. Recommend:
  **ownership-only ambiguity** (ranges recorded in evidence, not failure
  criteria) — the ownership-equivalence tie-break sentence is the more
  useful semantics and matches user story 11.
- Update ADR 0036's "uniquely aligned local hunk" to reference the
  enumeration, not the single alignment.

### (b) Fallback: amend PRD + ADR 0036 to the adjacency contract

If enumeration is declined, the contract text must be downgraded so it stops
promising what the code does not do. Proposed wording for PRD line 139:

> "Each paragraph is diffed with a single SequenceMatcher alignment. Equal
> units retain the baseline style of the aligned unit. Insertions and
> anchored mixed replacements between equal-valued, differently styled
> neighbors fail with `ambiguous-alignment`; all other insertions use the
> left/right caret context, and single-style replacements and deletions
> take the style ownership of the aligned occurrence. Alternative
> minimum-cost alignments are not enumerated; where repeated text with mixed
> styles is edited, ownership is deterministic but may not be unique."

And ADR 0036: replace "uniquely aligned local hunk" with "a local hunk whose
aligned neighbors do not straddle equal-valued, differently styled text".
This keeps behavior honest but **accepts silent wrong-style assignments**
(c3/c7/c9/c10/c11) for edits next to repeated text — which contradicts user
story 11 ("unchanged text around a rewrite keeps its original styles") and the
PRD's own failure-mode guarantee, so (a) is preferred.

## Appendix — repro

```text
# case c3 (one miss): baseline A(plain) A(bold), draft A X
python -m scripts extract src.docx -o wd     # src.docx: two runs "A","A", second bold
# edit.md body: A -> AX
python -m scripts edit sync wd
# -> synced; typed.md: A<span data-s="s_bold">X</span>  (X=bold, silent)
# SequenceMatcher opcodes: equal(0,1,0,1) replace(1,2,1,2)
```

Full harness with all 12 cases, the opcode dumps, and the enumeration oracle:
`C:\Users\Lin\AppData\Local\Temp\ambig_check\harness.py` (run `python
harness.py`; prints CLI result + `enum-{lcs,lev}` per case).

Key evidence files cited above (line numbers are from the main-repo checkout
at research time): `scripts/edit_sync.py:286-347` (`_assign_style`),
`:409-410` (single SequenceMatcher), `docs/prd/typed-mode-word-editing.md:139`
(Diff mapping), `docs/adr/0036-hash-bound-clean-edit-projection.md`
(mixed-replacement sentence).
