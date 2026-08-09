# Docx2typed Review Console Design System

## 1. Atmosphere & Identity

A quiet editorial workstation for reviewing document changes. The surface is paper-white, typographic, and deliberately Swiss: a strict grid, black ink, fine rules, one signal vermilion, and generous margins. The signature interaction is a sticky topbar plus fixed review rail: selecting one change moves the reader to the exact sentence while keeping the decision context visible.

## 2. Color

### Palette

| Role | Token | Light | Usage |
|------|-------|-------|-------|
| Canvas | `--canvas` | `#F1F0EC` | Application background |
| Paper | `--paper` | `#FBFAF7` | Document surface |
| Ink | `--ink` | `#111111` | Primary text and headings |
| Muted ink | `--ink-muted` | `#686761` | Metadata and secondary labels |
| Hairline | `--hairline` | `#D7D5CE` | Dividers and structure |
| Soft hairline | `--hairline-soft` | `#E7E5DE` | Subtle section separation |
| Signal | `--signal` | `#E34234` | Active navigation, destructive/delete state |
| Signal dark | `--signal-dark` | `#A9241B` | Signal hover/pressed |
| Cobalt | `--cobalt` | `#1646B8` | Focus, links, insert state |
| Insert wash | `--insert-wash` | `#E6EEF9` | Inserted text background |
| Delete wash | `--delete-wash` | `#F9E7E4` | Deleted text background |
| Comment wash | `--comment-wash` | `#F5E9C7` | Comment marker background |
| Success | `--success` | `#16704A` | Accepted decision |
| Warning | `--warning` | `#8A5A00` | Unmapped format notice |

### Rules

- Signal red is reserved for delete/destructive and the active index rule.
- Cobalt is reserved for insert/focus/link affordances.
- Surfaces are tonal, not card-heavy: one paper surface on one canvas.
- Every raw color in the generated page must trace to a token above.

## 3. Typography

### Scale

| Level | Size | Weight | Line Height | Usage |
|------|------|--------|-------------|-------|
| Display | `clamp(28px, 4vw, 48px)` | 700 | 1.0 | Document title |
| H1 | `22px` | 700 | 1.15 | Console title |
| H2 | `15px` | 700 | 1.25 | Rail groups and section labels |
| Body | `16px` | 400 | 1.75 | Document text |
| Body small | `13px` | 400 | 1.45 | Review cards and controls |
| Caption | `11px` | 700 | 1.25 | Overlines, counters, metadata |
| Mono | `11px` | 500 | 1.35 | Export schema and technical diagnostics only |

### Font Stack

- Chrome: `Arial, Helvetica, sans-serif` for the Swiss interface chrome.
- Source document: derived `font-family` from the canonical Word rPr registry; fallback `Arial, sans-serif`.
- Technical labels: `ui-monospace, SFMono-Regular, Consolas, monospace`.

The source document is allowed to retain its original Word typeface because fidelity is the product requirement; chrome remains limited to the interface stack.

## 4. Spacing & Layout

### Base Unit

All spacing derives from a base of **4px**.

| Token | Value | Usage |
|------|-------|-------|
| `--space-1` | `4px` | Icon gap and hairline offsets |
| `--space-2` | `8px` | Compact control groups |
| `--space-3` | `12px` | List row padding |
| `--space-4` | `16px` | Standard inset |
| `--space-5` | `20px` | Rail/card padding |
| `--space-6` | `24px` | Main document inset |
| `--space-8` | `32px` | Section breaks |
| `--space-10` | `40px` | Header and major rhythm |
| `--space-12` | `48px` | Document top spacing |
| `--space-16` | `64px` | Page-level breathing room |

### Grid

- Desktop: 12-column CSS grid; document stage spans 8 columns, review rail spans 4 columns.
- Max width: `1440px`; outer margins are `clamp(16px, 4vw, 64px)`.
- Rail: `minmax(280px, 360px)`, `position: sticky`, `top: calc(var(--topbar-height) + 24px)`, height `calc(100dvh - var(--topbar-height) - 48px)`.
- Mobile: one column; the rail remains below the sticky topbar and its own lists scroll independently.
- Breakpoints: `640px`, `900px`, `1200px`.

## 5. Components

### Console Header

- **Structure**: sticky topbar containing overline, document title, state summary, view toggle, export button, and file rule.
- **Variants**: final view / original view.
- **States**: default, hover, active, focus-visible, disabled.
- **Accessibility**: semantic header, buttons not divs, visible focus ring, no icon-only critical action.

### Document Stage

- **Structure**: paper surface, document header, paragraph stream, diagnostics details.
- **Variants**: final view, original view, active paragraph.
- **States**: default, focused revision, comment anchor; unsupported structural nodes are omitted from the reading path.
- **Accessibility**: document is readable in source order; revision marks expose `button` semantics through a labelled interactive element; source fonts remain readable at 16px minimum.

### Review Rail

- **Structure**: sticky aside, summary, tab switch (`修订`/`批注`), filter, indexed list; the comments tab separates original Word comments from new review notes.
- **Responsive variant**: desktop keeps the rail; phones replace it with a fixed overview ruler at the right edge. Vermilion markers represent revisions; warning markers represent comments.
- **Variants**: revisions, original comments, review notes, all/pending/decided filters.
- **States**: default, hover, active, accepted, rejected, pending, empty, mobile ruler marker active.
- **Accessibility**: `aside` landmark on desktop; mobile uses an accessible navigation landmark with labelled marker buttons and direct scroll-to-target behavior.
- **Motion**: selection scrolls with smooth motion and a temporary focus ring; reduced motion falls back to instant scroll.

### Decision Sheet
- **Structure**: selected change quote, author/date, accept/reject actions, optional note, apply/close.
- **Responsive variant**: on phones the decision sheet is a compact fixed bottom sheet with safe-area padding; adding a comment uses the same sheet surface.
- **States**: closed, open, selected action, saved.
- **Accessibility**: labelled controls, keyboard-reachable actions, safe-area-aware bottom placement, and focus moves to the active note field.

### Format Diagnostics

- **Structure**: closed `details` disclosure at the bottom of the document stage.
- **Variants**: all mapped, warning with unmapped features.
- **Rule**: technical style IDs, feature lists, and unsupported structural nodes are not shown in the primary reading path; diagnostics are opt-in.

## 6. Motion & Interaction

| Type | Duration | Easing | Usage |
|------|----------|--------|-------|
| Micro | `120ms` | `ease-out` | Button and row states |
| Standard | `220ms` | `ease-in-out` | Rail tab/filter transitions |
| Focus | `420ms` | `cubic-bezier(.16, 1, .3, 1)` | Scroll to selected revision |

- Animate only `transform`, `opacity`, and `outline-color` where possible.
- Revision selection uses `scrollIntoView({ behavior: "smooth", block: "center" })` and a bounded focus pulse.
- `prefers-reduced-motion: reduce` disables smooth scrolling and pulses.

## 7. Depth & Surface

### Strategy

**Mixed, restrained**: paper/canvas tonal shift plus one soft shadow for the sticky rail and decision sheet. No rounded-card grid and no gradients.

| Level | Value | Usage |
|------|-------|-------|
| Rail | `0 16px 36px rgba(17,17,17,.08)` | Fixed review rail separation |
| Sheet | `0 20px 56px rgba(17,17,17,.18)` | Decision dialog |

Rules: document paper keeps a 1px hairline; list rows rely on separators and active rules; controls use 0–6px radii only. The visual hierarchy comes from grid, typography, and alignment—not decorative effects.
