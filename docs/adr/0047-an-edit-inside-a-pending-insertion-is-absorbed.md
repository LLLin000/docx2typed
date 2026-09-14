# 0047 — An edit inside a pending insertion is absorbed, not nested

## Status

Accepted · 2026-09-14 · branch `feature/insert-absorption`

## Context

An inserted paragraph is represented by an **insertion on its paragraph mark**
(`w:pPr/w:rPr/w:ins`); its text is ordinary runs. Word shows it as one pending
insertion by its author.

Editing inside such a paragraph used to produce a nested tracked change — a
`w:del`/`w:ins` pair *inside* a paragraph the baseline never had. Two things
went wrong:

- the builder refuses that shape (`new paragraph cannot add structural tokens`),
  but the refusal ran at validate time, i.e. after the mutation had been
  written, so the workdir was left unbuildable (issue #83);
- the editing tools could not even reach the point of trying: the paragraph's
  inherited style was never materialised, so `format_span` died with an empty
  style id (fixed in the same work, PR #87).

Measured on the real patent workdir (同心圆, `实施例4`):

```
typed.md         <!--@p id="P96" inherit="P74"-->          (pending insertion)
document.xml     <w:p><w:pPr><w:rPr><w:ins w:author="照轩 林"/></w:rPr></w:pPr>
                 <w:r><w:t>…正文…</w:t></w:r>              (text is plain)
```

## Decision

**Editing a pending insertion by its own author rewrites the insertion in
place.** The insertion *is* the tracked change, so its content changes; no
revision is nested inside it. This is what Word produces when an author keeps
editing what they just inserted: one insertion whose content changed, not an
insertion containing another revision.

Concretely, in the sync layer:

| operation on a pending insertion | result |
|---|---|
| text edit by the insertion's author | in-place rewrite; the insertion stays one insertion |
| formatting by the insertion's author | restyle the insertion's own text in place |
| deletion of the whole paragraph | the paragraph disappears with its insertion mark — there is nothing in the baseline to delete, so no tombstone is written |
| edit by a **different** author | refused (`edit-inside-pending-insertion`): Word represents that by *splitting* the insertion at the edit point, and generating that shape needs its own evidence |
| insertion whose mark records no author | treated as absorbable (there is no other author to protect) |

Every absorbed hunk is labelled in the ledger (`"absorbed": "pending-insertion"`)
so the record stays honest about what kind of change it was.

The principle this follows, and which future work inherits:

> **Reading supports any shape (faithful replay). Generation produces only the
> simplest shape.**

Nested or split revisions arriving from Word are read and rebuilt; the engine
does not invent them.

## Consequences

- The refusal of issue #83 becomes an *absorption* for the author's own
  insertions, and a precise refusal (naming the other author, with the
  fallbacks) for everyone else's — no state is written in the refused case.
- `edit-inside-pending-insertion` stays registered as the stable negative; its
  `reason` distinguishes `different-author` from a nested-shape refusal.
- Formatting a pending insertion in direct mode keeps working (PR #87) and is
  now the same code path as the tracked case — the difference is only whether a
  session is in track mode at all.
- **Pending evidence**: the absorbed shape (insertion mark preserved, no
  nested `w:del`/`w:ins`, text changed) is asserted by
  `tests/test_insertion_chain.py` against the built package. Microsoft Word
  interoperability for the four cases (type / delete / format / whole-paragraph
  delete inside a pending insertion) still has to be recorded by the release
  office-evidence lane on a Word host; this ADR does not claim it as verified.
- The split-insertion shape (other author) remains unimplemented by design, and
  is refused with an actionable message rather than guessed.
