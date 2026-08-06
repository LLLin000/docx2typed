# UAX29-C1-1 segmentation gap assessment — issue #7

**Verdict: 32 of 60 probes diverge from full UAX29 extended grapheme clusters, across 8 rule classes (6 split-direction, 2 over-merge). Zero divergence for the tool's real corpus (Chinese/English patent prose). Recommendation: (b) add the `regex` dependency (`regex>=2024.7.24`), one-line swap, pin tables to Unicode 16.0.0.**

Date: 2026-08-06. Environment: Windows 11, Python 3.14.0 (stdlib `unicodedata` = Unicode 16.0.0), `regex` 2026.6.28 (oracle). Method: approximation copied **verbatim** from `scripts/edit_sync.py:85-147` into a throwaway script (temp dir, not the repo); oracle = `regex.findall(r"\X", s)`. `regex` implements the full default EGC rules incl. GB9c (InCB) since release 2024.7.24 (mrab-regex git issue #535, "Implement GB9c rule to handle Unicode 15.1 GraphemeBreakTest"; changelog/compare 2024.5.15...2024.7.24).

## 1. The contract vs the implementation

- PRD `docs/prd/typed-mode-word-editing.md:138` pins the unit: "Synchronization uses Unicode extended grapheme clusters under UAX #29 conformance clause UAX29-C1-1, pinned for this contract to Unicode 16.0.0… It must not split combining sequences, emoji ZWJ sequences, flag sequences…". Header/evidence record `segmentation="uax29-c1-1/unicode-16.0.0"` (PRD :58, :127). PRD :218: "This project pins the clean-sync segmentation contract to UAX29-C1-1 with the repository's Unicode 16.0.0 catalog."
- ADR `docs/adr/0036-hash-bound-clean-edit-projection.md:13`: sidecar binds "the segmentation contract"; sync validates units against it.
- Implementation `scripts/edit_sync.py:100-147` `grapheme_clusters()`: GB3 (CRLF), GB9/GB9a (Mn/Me/Mc continuation), GB11 (ZWJ-adjacency, unqualified), GB12/13 (RI pairs) via `unicodedata.category`. Docstring concedes "not a claim of full UAX29 conformance for exotic scripts."
- Both flatteners use the same function (`flatten_paragraph` :180, `flatten_edit_body` :209/:212), so sync is **internally consistent** — the gap is contract-vs-code and tool-vs-Word (Word/ICU use full UAX29), not a self-corruption bug.

## 2. Break-list: 32 divergent probes in 8 rule classes

Notation: `X` = cluster break. Direction is relative to full UAX29: **split** = approximation makes *more, smaller* clusters (can split one visual grapheme across style runs); **over-merge** = approximation makes *fewer, larger* clusters.

### Class 1 — GB9c Indic conjuncts (missing rule; **split**) — 11 probes
Approximation joins `consonant × virama` (virama is Mn) but stops there; full UAX29 additionally joins `virama × consonant` via GB9c (`InCB`):

| Input (codepoints) | Approximation | Full UAX29 (`\X`) |
|---|---|---|
| क्ष `0915 094D 0937` | `[0915 094D] [0937]` | `[0915 094D 0937]` |
| द्ध `0926 094D 0927` | `[0926 094D] [0927]` | `[0926 094D 0927]` |
| क्षेत्र `0915 094D 0937 0947 0924 094D 0930` | `[0915 094D] [0937 0947] [0924 094D] [0930]` | `[0915 094D 0937 0947] [0924 094D 0930]` |
| कर्म `0915 0930 094D 092E` | `[0915] [0930 094D] [092E]` | `[0915] [0930 094D 092E]` |
| Bengali ক্ত `0995 09CD 09A4` | `[0995 09CD] [09A4]` | `[0995 09CD 09A4]` |
| Telugu క్ష `0C15 0C4D 0C37` | `[0C15 0C4D] [0C37]` | `[0C15 0C4D 0C37]` |
| Malayalam ക്ഷ `0D15 0D4D 0D37` | `[0D15 0D4D] [0D37]` | `[0D15 0D4D 0D37]` |
| Extended conjunct `0915 094D 1AB0 0937` | `[0915 094D 1AB0] [0937]` | `[0915 094D 1AB0 0937]` |
| Hindi sentence "यह क्षेत्र का परीक्षण द्धि है।" | every conjunct split (7 extra boundaries) | conjuncts whole |

Not divergent (full UAX29 also splits — InCB not assigned there): Tamil க்ஷ `0B95 0BCD 0BB7`, Kannada ಸ್ಸ `0CB8 0CCD 0CB8`, Gurmukhi ਕ੍ਖ `0A15 0A4D 0A16`, Sinhala ක්ර `0D9A 0DCA 0DBB`.

**Consequence for edit sync:** every Devanagari/Bengali/Telugu/Malayalam conjunct is 2 units where the contract (and Word) see 1. A replacement at that boundary can assign different styles to the two halves of one visible conjunct; the diff gains alignment points → higher `ambiguous-alignment` surface.

### Class 2 — GB10 emoji modifiers (missing rule; **split**) — 4 probes
U+1F3FB–U+1F3FF are `Sk`, not Extend, so the modifier detaches:

| Input | Approximation | Full UAX29 |
|---|---|---|
| 👋🏻 `1F44B 1F3FB` | `[1F44B] [1F3FB]` | `[1F44B 1F3FB]` |
| ☝🏽 `261D 1F3FD` | `[261D] [1F3FD]` | `[261D 1F3FD]` |
| 👩🏽💻 `1F469 1F3FD 200D 1F4BB` | `[1F469] [1F3FD 200D 1F4BB]` | `[1F469 1F3FD 200D 1F4BB]` |
| "结果: 👍🏻 ✔️" (realistic sentence) | `[… 1F44D] [1F3FB …]` | `[… 1F44D 1F3FB …]` |

**Consequence:** skin-tone emoji split into base + modifier units; each half can take a different style; Word treats them as one unit.

### Class 3 — GB9/GB9a over-applied after Control (wrong rule; **over-merge**) — 4 probes
Full UAX29 never lets Extend/SpacingMark attach to a Control (GB9 excludes X=Control); the approximation doesn't check:

| Input | Approximation | Full UAX29 |
|---|---|---|
| `61 000A 0301` (a, LF, combining acute) | `[61] [000A 0301]` | `[61] [000A] [0301]` |
| `61 000D 000A 0301` | `[61] [000D 000A 0301]` | `[61] [000D 000A] [0301]` |
| `61 200B 0301 62` (a, ZWSP, acute, b) | `[61] [200B 0301] [62]` | `[61] [200B] [0301] [62]` |
| `61 2060 0301` (a, WJ, acute) | `[61] [2060 0301]` | `[61] [2060] [0301]` |

**Consequence:** a combining mark after newline/ZWSP/WJ is fused into the control's unit; the mark cannot start its own style run. Coarser units only — no cluster is split.

### Class 4 — GB11 ZWJ over-applied (wrong rule; **over-merge**) — 3 probes
The approximation merges ZWJ with *any* neighbor; full GB11 only joins `Extended_Pictographic Extend* ZWJ × Extended_Pictographic`:

| Input | Approximation | Full UAX29 |
|---|---|---|
| `61 200D 62` (a, ZWJ, b) | `[61 200D 62]` | `[61 200D] [62]` |
| `1F468 200D 78` (👨, ZWJ, x) | `[1F468 200D 78]` | `[1F468 200D] [78]` |
| `200D 61` (leading ZWJ, a) | `[200D 61]` | `[200D] [61]` |

Real emoji ZWJ sequences (👨👩👧👦, ❤️🔥, keycaps, VS16 chains) are **correct** — divergence only with stray/malformed ZWJ at boundaries (the case the ticket names).

### Class 5 — Extend set miss: ZWNJ/FF9E/FF9F (missing Extend chars; **split**) — 4 probes
`_is_extend` = Mn/Me; UAX29 GCB=Extend also covers ZWJ (special-cased), **ZWNJ U+200C (Cf)**, and halfwidth katakana voiced marks **U+FF9E/U+FF9F (Lm)**:

| Input | Approximation | Full UAX29 |
|---|---|---|
| `61 200C 62` (a, ZWNJ, b) | `[61] [200C] [62]` | `[61 200C] [62]` |
| Persian میروم `0645 06CC 200C 0631 0648 0645` | `[0645] [06CC] [200C] [0631 0648 0645]` | `[0645] [06CC 200C] [0631 0648 0645]` |
| ガ `30AB FF9E` | `[30AB] [FF9E]` | `[30AB FF9E]` |

**Consequence:** ZWNJ is an ordinary orthographic character in Persian — every ZWNJ-bearing word gains an extra boundary (split direction).

### Class 6 — GB6/7/8 Hangul jamo (missing rule; **split**) — 3 probes
`L × V`, `LV × V`, `V × T`, `LVT × T` not implemented; jamo are `Lo` so they never join:

| Input | Approximation | Full UAX29 |
|---|---|---|
| 가 `1100 1161` | `[1100] [1161]` | `[1100 1161]` |
| 각 `1100 1161 11A8` | `[1100] [1161] [11A8]` | `[1100 1161 11A8]` |
| 한글 `1112 1161 11AB 1100 1173 11AF` | 6 units | `[1112 1161 11AB] [1100 1173 11AF]` |

### Class 7 — GB9b Prepend (missing rule; **split**) — 3 probes
| Input | Approximation | Full UAX29 |
|---|---|---|
| Arabic number sign `0600 0661 0662` | `[0600] [0661] [0662]` | `[0600 0661] [0662]` |
| Malayalam reph ക... `0D4E 0D15` | `[0D4E] [0D15]` | `[0D4E 0D15]` |
| Malayalam word with reph `0D15 0D47 0D30 0D4E 0D32 0D02` | reph detached | reph fused to following letter |

### Class 8 — GCB=SpacingMark beyond Mc (category miss; **split**) — 2 probes
U+0E33 (Thai AM) and U+0EB3 (Lao AM) are `Lo` but GCB=SpacingMark; the approximation keys off `Mc`:

| Input | Approximation | Full UAX29 |
|---|---|---|
| กำ `0E01 0E33` | `[0E01] [0E33]` | `[0E01 0E33]` |
| ຢຳ `0EA2 0EB3` | `[0EA2] [0EB3]` | `[0EA2 0EB3]` |

## 3. Verified conformant (28/60 probes, byte-identical)

CJK/EN patent prose (all baseline probes); combining sequences ä é a̧; CJK + combining; keycaps 1️⃣ #️⃣; flags 🇨🇳, 🇨🇳🇺🇸, RI with gap, RI with combining between; ❤️ VS16; emoji ZWJ family 👨👩👧👦; ❤️🔥; 👍; CRLF; ZWSP between words; Tamil/Kannada/Gurmukhi/Sinhala conjuncts (both split — equal); Mongolian vowel separator (both split); Thai Mc vowel sign.

## 4. Does it matter for the tool's corpus?

| Class | In CN/EN patent prose? | In general use of a DOCX editor? |
|---|---|---|
| 1 GB9c conjuncts | no (needs Devanagari-family script) | yes — any Hindi/Sanskrit/Bengali/Telugu/Malayalam document; **every conjunct** |
| 2 Emoji modifiers | no (patents have no emoji) | yes — any pasted chat/comment with skin-tone emoji |
| 3 GB9 after Control | ~no (needs combining mark after LF/ZWSP/WJ — paste-corruption edge) | rare |
| 4 ZWJ over-link | ~no (stray-ZWJ paste edge) | rare but real (malformed emoji copy-paste) |
| 5 ZWNJ/FF9E-FF9F | no | yes for Persian text (ZWNJ is a regular letter) |
| 6 Hangul jamo | no | rare (Korean text is precomposed) |
| 7 Prepend | no | Arabic number signs, Malayalam |
| 8 Thai/Lao AM | no | yes for Thai/Lao text |

**CJK/EN patent corpus: 0 divergences observed.** The approximation is exact for the tool's actual corpus. The gap is latent: it fires the moment the tool edits a non-CJK/EN paragraph or pasted emoji.

## 5. Costed recommendation — **(b) add `regex`, pinned**

- **(a) keep + document boundary** — $0. The docstring already documents the boundary, but the *contract* (PRD :138/:218, ADR 0036, header `uax29-c1-1/unicode-16.0.0`) overclaims, and the Word matrix ticket operates on the same units — Word (ICU) is full UAX29, so caret-matrix tests with skin-tone emoji/Indic text would disagree with the tool. Tenable only if the contract wording is softened and the corpus hard-constrained.
- **(b) `regex` dependency** — **recommended**. One-line swap: `grapheme_clusters(text)` returns `regex.findall(r"\X", text)` (keep wrapper + docstring; both flatteners untouched). Pin `regex>=2024.7.24` (first release passing Unicode 15.1 GraphemeBreakTest incl. GB9c; current 2026.6.28 in use). Pure-Python package, no build deps, de-facto standard for `\X`. Kills all 8 classes at once. Cost: one `pyproject.toml` line + re-run of the existing Unicode fixture suite. Unicode-version note: regex tracks Unicode per release — record the table-version correspondence at pin time so the 16.0.0 contract stays truthful (Unicode 16.0 published 2024-09; use a post-16.0 regex release).
- **(c) implement remaining rules** — ~150–250 lines + vendored property tables (InCB from DerivedCoreProperties, Extended_Pictographic/E_Base from emoji-data, Prepend set, Hangul ranges, {ZWNJ, FF9E, FF9F, 0E33, 0EB3} hardcodes) + tests + regeneration per Unicode bump. Highest maintenance, zero new deps. Not worth it while (b) costs one line.
- **Future path:** CPython 3.15 adds full UAX29 segmentation in the stdlib (`unicodedata.iter_graphemes`, plus `grapheme_cluster_break`/`indic_conjunct_break`). When `requires-python` reaches 3.15, swap `regex` for the stdlib and drop the dependency; until then (b) is the minimum.

**Decision: (b).** 32/60 probes diverge across 8 rule classes; 6 of 8 classes split clusters (the dangerous direction for style ownership); the fix is a one-line swap plus one pure-Python pin that makes the pinned contract truthful and aligns the tool with Word's units for the caret/Word-matrix work.

## 6. Reproduction

Probe script (approximation verbatim + `regex.findall(r"\X", …)` over 60 inputs) was run from a temp dir to avoid touching either repo; inputs and outputs are fully listed in section 2. Rerun: `python -c "import regex; regex.findall(r'\X', s)"` vs `scripts.edit_sync.grapheme_clusters(s)`.
