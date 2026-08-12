# Cross-platform DOCX interoperability evidence

Research note for Wayfinder ticket #30 ("Research cross-platform DOCX
interoperability evidence"). Feeds the downstream decision ticket (#42, freeze
the interoperability matrix and corpus governance). This note records
primary-source-backed mechanisms and does **not** decide the final
compatibility matrix.

## Question

What primary-source-supported mechanisms can produce reproducible release
evidence that docx2typed outputs open, render, save, and round-trip without
repair in Word on Windows, Word on macOS, and LibreOffice? Cover automation/CI
constraints, relevant version differences, modern comments and revision
dialects, fixture licensing/privacy, and public synthetic versus private
real-document corpus patterns. Return facts and feasible evidence patterns.

## Evidence classes

The ticket conflates four claims that have very different automation ceilings.
Any qualification design should keep them separate:

| Class | Question | Automation ceiling |
|---|---|---|
| Open check | The consumer opens the file and reports success | Automatable (LibreOffice fully; Word only with a licensed desktop install) |
| Render check | The consumer's layout engine renders content | Automatable where Word/LO run; PDF export is the documented artifact |
| Save round-trip | Open → save → reopen preserves content/comments/revisions | Automatable where Word/LO run; semantic (never byte) comparison |
| Repair-free | No silent repair occurred on open | **Not observable through any documented API**; needs human/desktop gate or proxies (see "Repair semantics") |

## Verified facts

### Automation surfaces

**Word on Windows — COM object model (no headless mode).**
- `Documents.Open(FileName, ..., OpenAndRepair, ...)` is documented; `OpenAndRepair:=True` explicitly repairs the document on open (default `False`).
  [Documents.Open method (Word) — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/api/word.documents.open)
- `Application.DisplayAlerts` and `Application.AutomationSecurity` are the documented controls for suppressing dialogs and macro/active-content behavior in unattended runs.
  [Considerations for unattended automation of Office in the M365 for unattended RPA environment — Microsoft Learn](https://learn.microsoft.com/en-us/office/client-developer/integration/considerations-unattended-automation-office-microsoft-365-for-unattended-rpa)
  [Application.DisplayAlerts method (Word) — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/api/word.application.displayalerts)
- `Document.SaveAs2(FileName, FileFormat, ..., CompatibilityMode)` is the documented save-as surface (docx = `wdFormatXMLDocument` family); `CompatibilityMode` lets the harness observe/force the compatibility mode Word uses.
  [Document.SaveAs2 method (Word) — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/api/word.saveas2)
- `Document.ExportAsFixedFormat(...)` exports PDF/XPS through Word's own layout engine — a documented, reproducible render artifact on Windows.
  [Document.ExportAsFixedFormat method (Word) — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/api/word.document.exportasfixedformat)
- Word's command line is not a conversion surface: documented switches (`/a` start without add-ins/templates, `/q` no splash, `/mMacroName` run a macro from `Normal.dotm` on startup) require an interactive desktop and provide no headless convert.
  [Command-line switches for Microsoft Office products — Microsoft Support](https://support.microsoft.com/en-us/office/command-line-switches-for-microsoft-office-products-079164cd-4ef5-4178-b235-441737deb3a6)
- Microsoft's support policy: server-side/non-interactive automation of Office is **not supported** ("Office may exhibit unstable behavior and/or deadlock"); Office assumes an interactive desktop session with a user profile. Unattended runs are "AS IS"; Microsoft's documented mitigations are `DisplayAlerts`, `AutomationSecurity`, instance/VM isolation, and restart-based resiliency.
  [Considerations for server-side Automation of Office — Microsoft Support](https://support.microsoft.com/topic/considerations-for-server-side-automation-of-office-48bcfe93-8a89-47f1-0bce-017433ad79e2)
  [Considerations for unattended automation of Office — Microsoft Learn](https://learn.microsoft.com/en-us/office/client-developer/integration/considerations-unattended-automation-office-microsoft-365-for-unattended-rpa)

**Word on macOS — AppleScript (no COM).**
- Word for Mac supports VBA and AppleScript (`osascript`); Office 2016 for Mac is sandboxed, and the documented bridge commands are `AppleScriptTask` (call an AppleScript file from VBA) and `MacScript` (deprecated). Third-party COM add-ins are not supported on Mac.
  [VBA on Office for Mac overview — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/api/overview/office-mac)
  [AppleScriptTask function — Microsoft Learn](https://learn.microsoft.com/en-us/office/vba/office-mac/applescripttask)
- Documented osascript pattern for open→save-as-docx: `save as active document file name <path> file format format document` (Word's native .docx container); saving as legacy binary uses `format document97`. The same Microsoft Q&A thread documents real failure modes (scripts producing a zip that older Word treats as damaged), i.e. the AppleScript save path itself needs qualification.
  [AppleScript save as "original" Word document — Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/4834039/applescript-save-as-original-word-document)
- Word for Mac PDF export through the object model is **not confirmed** (see Unresolved unknowns); `ExportAsFixedFormat` is documented without a platform restriction but Mac availability must be verified on the target build.
- macOS activation constraint: shared computer activation (the mechanism Microsoft documents for ephemeral/shared Windows machines) is **not available for Office for Mac**.
  [Overview of shared computer activation — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365-apps/licensing-activation/overview-shared-computer-activation)

**LibreOffice — fully headless.**
- `soffice --headless --convert-to <ext>[:<filter>[:<params>]] --outdir <dir> <files>` is the documented batch conversion surface; `--invisible`, `--norestore`, `--nolockcheck`, and `-env:UserInstallation=file:///...` (isolated user profile) are documented for unattended/CI use.
  [Starting LibreOffice Software With Parameters — LibreOffice Help](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)
- The docx export filter is documented as **"Office Open XML Text"** (API name; media type `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, extension docx); PDF export filter is `writer_pdf_Export`. This is the filter-name source for `--convert-to docx` and `--convert-to pdf` and for re-saving a docx through LO (a LO round-trip evidence path).
  [File Conversion Filter Names — LibreOffice Help](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)
- UNO API (`com.sun.star.document` etc.) is the documented programmatic surface for load/save with error handling beyond plain convert-to.
  [LibreOffice API — api.libreoffice.org](https://api.libreoffice.org/)
- `unoserver` is a maintained listener-mode wrapper (documented ~50–75% CPU reduction by keeping LibreOffice resident) that surfaces conversion results to CI; useful for batch open/save loops, but it is a third-party tool, not TDF-supported.
  [unoserver — GitHub](https://github.com/unoconv/unoserver)
- LibreOffice 25.2 release notes (current stable line as of this note) continue to fix DOCX import/export (headers/footers, CJK display, protection) — evidence that LO's OOXML fidelity is a moving target that must be pinned to a version in evidence records.
  [LibreOffice 25.2 Release Notes — The Document Foundation Wiki](https://wiki.documentfoundation.org/ReleaseNotes/25.2)

**Cloud, non-desktop alternatives (for completeness).**
- Microsoft Graph `GET /drive/items/{id}/content?format=pdf` converts docx→pdf without installing Office. It renders through Microsoft's online pipeline, **not** desktop Word, so it is at best a "renders in M365 web" smoke test and must not be labeled as a Word-desktop statement.
  [Convert to other formats — Microsoft Graph](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content-format)

### Word desktop platform differences relevant to qualification

- Product families that must be distinguished in any matrix: Microsoft 365 (continuous feature updates; current supported OS per Microsoft is Windows 11 / Windows Server 2022-2025 and "one of the three most recent versions of macOS"), and perpetual LTSC lines (Office LTSC 2021, LTSC 2024; Office 2019 for Mac reached end of support 2023-10-10).
  [System requirements for Microsoft 365 for business, education and government use — Microsoft Support](https://support.microsoft.com/en-us/office/system-requirements/system-requirements-for-microsoft-365-for-business-education-and-government-use)
  [Deployment guide for Office for Mac — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365-apps/mac/deployment-guide-for-office-for-mac)
- Microsoft 365 Apps on Windows Server is supported **only while the server version is in mainstream support** (e.g. Server 2022 through 2026-10); relevant if a self-hosted Windows CI host is proposed.
  [Windows Server end of support and Microsoft 365 Apps — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365-apps/end-of-support/windows-server-support)
- No Microsoft source was found that asserts layout/pagination parity between Word for Mac and Word for Windows (fonts, text shaping, and printer drivers differ per platform). Qualification should therefore treat each platform's open/save as independent evidence and not extrapolate render results across platforms (see Unresolved unknowns).

### Modern comments and revision dialects

- Classic comments live in `word/comments.xml` (ISO/IEC 29500). Since Word 2013, Word also writes an extensible comments part (`w15:commentsEx` / `CT_CommentsEx`, namespace `http://schemas.microsoft.com/office/word/2012/wordml`), defined in the MS-DOCX specification; threaded ("modern") comments additionally rely on the people part.
  [MS-DOCX: CT_CommentsEx — Microsoft Open Specifications](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/4add9f34-fdba-4324-a9d6-60e78897c5a6)
  [MS-DOCX: Word Extensions to the Office Open XML File Format (specification home) — Microsoft Learn](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/b839fe1f-e1ca-4fa6-8c26-5954d0abbccd)
  [WordprocessingPeoplePart class (Open XML SDK) — Microsoft Learn](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.packaging.wordprocessingpeoplepart)
- Threaded/modern comments are documented as a **Microsoft 365 feature** (Word for Microsoft 365 on Windows and macOS; replies, @mentions, resolution), i.e. not a guaranteed capability of perpetual LTSC licenses.
  [Collaborate with comments in Office 365 — Microsoft Support](https://support.microsoft.com/en-us/office/collab-files/collaborate-with-comments-in-office-365)
- Revision containers (`w:ins`/`w:del`) are core ISO/IEC 29500; Word 2013+ extensions add `w15`/`w16`/`w16du` attributes (including the `w16du:dateUtc` revision-date attribute this project already gates in ADR 0037). The full extension vocabulary, including the `word16du` schema, is listed in MS-DOCX Appendix A.
  [MS-DOCX: Appendix A: Full XML Schemas — Microsoft Learn](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/d0a2e301-0ff7-4e9e-9bb7-ff47070dce0a)
- LibreOffice: no source found stating support for Word's threaded-comment OOXML (`commentsExtensible`/`people`) import or export. LO 25.2 has its own reply-comment model ("promote a reply comment into a root comment" in Writer release notes), which is **not** evidence of OOXML threaded-comment compatibility. This is a fixture-level open question (see Unresolved unknowns).
  [LibreOffice 25.2 Release Notes — The Document Foundation Wiki](https://wiki.documentfoundation.org/ReleaseNotes/25.2)

### Repair semantics and "without repair" detection

- "Open and Repair" is a documented user command (File > Open > arrow > Open and Repair); Word's recovery flows (including the "found unreadable content" prompt) are documented in Microsoft's damaged-document guidance.
  [Open a document after a file corruption error — Microsoft Support](https://support.microsoft.com/en-us/word/open-a-document-after-a-file-corruption-error)
  [How to troubleshoot damaged documents in Word — Microsoft Learn](https://learn.microsoft.com/en-us/previous-versions/troubleshoot/microsoft-365/microsoft-365-apps/word/damaged-documents-in-word)
- There is **no documented COM/AppleScript property or event that reports "this file was repaired"**. Silent repair can therefore not be asserted from an automation success alone. Practical documented proxies:
  - Pre-open schema/package validation with `OpenXmlValidator` (Open XML SDK, `FileFormatVersions`-targeted) — catches package/schema violations that Word commonly repairs; note schema-valid is not Word-accept.
    [OpenXmlValidator class — Microsoft Learn](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.validation.openxmlvalidator)
  - Open → save → reopen stability: reopen the Word-saved copy and re-run the project's own verify signatures (text, revision structure, comments) — catches content loss, not silent repairs of tolerated markup.
  - A human/desktop gate that opens the file interactively and records whether the unreadable-content prompt appears (see below).
- Word rewrites the package on save (rsid attributes, core/app properties, zip structure), so **byte-level comparison across a Word save is meaningless**. The repo already reflects this: release fixtures are hashed rsid-normalized (commit 34b98f1; `corpus/release/model-manifest.json`), and verification.md defines the interop bar as "conversion must complete without repair warnings" plus structural, not byte, equality.

### Runner/CI realities and licensing

- GitHub-hosted runners are VMs (Windows Server 2022/2025 images, macOS Sonoma/Sequoia images, Ubuntu), maintained in `actions/runner-images`; the hosted-runner docs make no guarantee about Office install, GUI-session reliability, or RDP access. Office-on-runner automation is therefore a verify-first pattern, not a documented capability.
  [About GitHub-hosted runners — GitHub Docs](https://docs.github.com/en/actions/using-github-hosted-runners/about-github-hosted-runners)
- Windows: unattended Office install is documented via the Office Deployment Tool; activation on shared/ephemeral machines is documented via shared computer activation (M365 plans; token renewal, internet required).
  [Overview of shared computer activation — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365-apps/licensing-activation/overview-shared-computer-activation)
- macOS: no SCA; activation requires a signed-in M365 account or volume-license serialization (per the Office for Mac deployment guide). This makes fully automated Word-for-Mac runs on ephemeral CI materially harder than Word-for-Windows runs.
  [Deployment guide for Office for Mac — Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365-apps/mac/deployment-guide-for-office-for-mac)
- LibreOffice on Linux CI is the only consumer with a fully documented, zero-license headless path — this repo already uses it (`release-qualification.yml` installs `libreoffice-writer` on ubuntu; `scripts/release_acceptance.py` runs `soffice --headless --convert-to pdf` and skips when soffice is absent; verification.md documents the Windows-side human gate with native drive paths).

### Fixture corpora: licensing and privacy

- Public synthetic corpus options with permissive licenses, suitable for committing to CI:
  - python-docx test files — MIT.
    [python-docx LICENSE — GitHub](https://github.com/python-openxml/python-docx/blob/master/LICENSE)
  - Open XML SDK repo (contains validation and test documents) — MIT.
    [Open-XML-SDK LICENSE — GitHub](https://github.com/OfficeDev/Open-XML-SDK/blob/main/LICENSE)
  - LibreOffice core regression fixtures under `sw/qa/.../data/` — MPL-2.0.
    [LibreOffice licenses — libreoffice.org](https://www.libreoffice.org/licenses/)
  - Apache POI test data — Apache-2.0 project; per-file provenance can vary (some fixtures are third-party documents donated with permission), so each imported fixture needs a notice check before committing.
    [Apache POI — poi.apache.org](https://poi.apache.org/)
  - ECMA-376 / ISO/IEC 29500 itself is freely downloadable and can be used to generate spec-conformant fixtures without third-party content.
    [ECMA-376 — Ecma International](https://ecma-international.org/publications-and-standards/standards/ecma-376/)
- Word-generated fixtures carry author metadata (core.xml properties, rsids, author strings in revisions/comments) — strip or synthesize it before redistribution; embedding proprietary fonts/images inside fixtures is a redistribution risk.
- Private real-document corpus: the repo already separates public synthetic fixtures (`corpus/release/`, committed; gitignored `corpus/real/`) from private real documents (dev corpus on `D:/L/AppData`, ADR 0024: "rendering screenshots are secondary evidence; package/XML invariants and independent typed verification are the acceptance gate"). Privacy pattern: keep real documents out of public CI entirely; redact/consent before any external disclosure; run the real-document gate on a controlled desktop.
- Modern-comments and revision-bearing fixtures are the hard case: there is no public corpus of Word 365 threaded-comment files with a permissive license identified in this research; they must be generated with a licensed Word install (private) or hand-built from MS-DOCX (synthetic).

## Feasible qualification designs (menu)

For each consumer, the evidence classes that are realistically automatable:

| Consumer | Open check | Render check | Save round-trip | Repair-free (silent) |
|---|---|---|---|---|
| LibreOffice (Linux CI, headless) | `soffice --headless --convert-to pdf` exit + artifact (already in CI) | PDF via `writer_pdf_Export` | `--convert-to docx` re-save, then LO/Word reopen + verify signatures | Not asserted; LO tolerance differs from Word (no documented "repair" signal) |
| Word on Windows | COM `Documents.Open` with `OpenAndRepair:=False` + `DisplayAlerts` off, no exception; needs licensed Office in an interactive session (self-hosted runner or controlled desktop) | `ExportAsFixedFormat` to PDF | `SaveAs2` to new docx → reopen → semantic verify + `OpenXmlValidator` | Not observable via API; human/desktop gate observes the unreadable-content prompt |
| Word on macOS | AppleScript open (osascript) in a logged-in session; needs licensed Word (no SCA) | PDF export unconfirmed (see unknowns) | AppleScript `save as ... format document` → reopen → semantic verify | Human/desktop gate |

Cross-cutting design options:

1. **Tiered evidence**: CI tier = LibreOffice open/render + OpenXmlValidator + project verify (automated, public fixtures, every release); Desktop tier = Word on Windows and macOS open/save/render on licensed machines (release checklist with recorded version/build numbers, per-fixture results); Corpus tier = private real documents only on the desktop tier.
2. **Cross-producer round-trip**: LO re-save (`--convert-to docx`) and Word re-save, each followed by reopen in the other consumer plus the project's verify signatures — the strongest automated proxy for "other producers accept our output" short of Word itself.
3. **Semantic, not byte, round-trip**: compare final-text/revision/comment signatures (the repo's existing verify layers) across open→save→reopen; record Word/LO build numbers in the evidence record (M365 continuous updates make unversioned results unreproducible).
4. **Seeded-corruption calibration**: a small fixture set with deliberately broken packages validates that each automated open check actually fails when it should (guards against a harness that passes everything). LO's headless failure mode on corrupt docx (exit code vs. partial output) must be established first (see unknowns).
5. **Human/desktop gate**: the only documented way to capture "no repair" — interactive open in Word (Win + Mac) observing whether the unreadable-content prompt appears, plus a visual render check; recorded as screenshots + version matrix in the release artifact.
6. **Privacy-safe corpus policy**: commit only permissively licensed synthetic fixtures; keep real documents private; strip author metadata from any Word-generated fixture; license-review each third-party fixture (POI in particular).

## Not automatable / fragile (explicit)

- **Silent-repair detection in Word**: no documented API surface; "opened without exception" is necessary, not sufficient, evidence.
- **Office in non-interactive sessions**: explicitly unsupported by Microsoft (deadlock/instability risk); every Word automation must run in a logged-in interactive session.
- **Word for Mac on ephemeral CI**: no SCA, VL/sign-in activation required; runner GUI-session reliability is undocumented.
- **Word for Mac PDF export** via object model: unconfirmed.
- **Cross-platform render parity**: no Microsoft statement exists; results are platform-specific by construction.
- **LO round-trip fidelity for tracked changes/comments**: partial and version-dependent; must be pinned and fixture-tested, not assumed.
- **Graph/web conversion** as Word evidence: renders via the M365 online pipeline, not desktop Word; usable only as a web smoke test.

## Recommendation inputs for the downstream ticket (#42)

- Freeze a three-axis matrix: consumer (Word Win / Word Mac / LO) × evidence class (open / render / save-roundtrip / repair-free) × corpus tier (public synthetic / private real), with per-cell automation status as in the tables above.
- Word-family evidence realistically requires a licensed desktop (self-hosted runner or human gate); LO and OpenXmlValidator are the only fully automated pillars today.
- Pin and record versions in every evidence record: Word M365 build (or LTSC year), LO release, runner image label, and the repo commit under test.
- Treat render evidence as platform-specific; do not extrapolate Word-for-Mac results from Word-for-Windows.
- Keep the private real-document corpus out of public CI; the existing `corpus/release` vs `corpus/real` split already supports this.
- Modern-comments and threaded-revision qualification needs a dedicated fixture decision (no public licensed corpus found; Word-365-generated private fixtures or MS-DOCX-synthesized files).

## Unresolved unknowns

- Whether the "found unreadable content" recovery prompt is suppressible by `DisplayAlerts:=wdAlertsNone` in automation, and whether Word silently repairs when it is suppressed — no documentation found; must be measured on a licensed build with seeded-corruption fixtures.
- Word for Mac: does the object model (VBA/AppleScript) expose PDF export (`ExportAsFixedFormat` / `save as` PDF) on current builds? Not confirmed from documentation.
- LibreOffice: import/export status of Word 2013+ extensible-comments parts (`w15:commentsEx`, `commentsExtensible`, `people`) and of `w16du:dateUtc` revisions at the pinned LO version — not stated in release notes; needs a concrete fixture test.
- GitHub-hosted macOS/Windows runner behavior when running Office (GUI session, activation prompts, hang risk) — undocumented; verify on the actual image before relying on it.
- LibreOffice headless failure semantics on corrupt docx input (nonzero exit vs. partial artifact) — needs a seeded-corruption experiment before the LO open check can be made fail-closed.
- Public permissively-licensed fixtures containing real Word 365 threaded comments or move/conflict revisions — none identified; generation or synthesis required.

## Sources

Primary sources cited inline above; key ones collected here.

- Word VBA: [Documents.Open](https://learn.microsoft.com/en-us/office/vba/api/word.documents.open), [DisplayAlerts](https://learn.microsoft.com/en-us/office/vba/api/word.application.displayalerts), [SaveAs2](https://learn.microsoft.com/en-us/office/vba/api/word.saveas2), [ExportAsFixedFormat](https://learn.microsoft.com/en-us/office/vba/api/word.document.exportasfixedformat)
- Office automation support policy: [server-side Automation of Office (KB)](https://support.microsoft.com/topic/considerations-for-server-side-automation-of-office-48bcfe93-8a89-47f1-0bce-017433ad79e2), [unattended automation for M365 RPA](https://learn.microsoft.com/en-us/office/client-developer/integration/considerations-unattended-automation-office-microsoft-365-for-unattended-rpa)
- Word for Mac scripting: [VBA on Office for Mac](https://learn.microsoft.com/en-us/office/vba/api/overview/office-mac), [AppleScriptTask](https://learn.microsoft.com/en-us/office/vba/office-mac/applescripttask), [AppleScript save-as thread (Microsoft Q&A)](https://learn.microsoft.com/en-us/answers/questions/4834039/applescript-save-as-original-word-document)
- Word command line: [Command-line switches for Microsoft Office products](https://support.microsoft.com/en-us/office/command-line-switches-for-microsoft-office-products-079164cd-4ef5-4178-b235-441737deb3a6)
- Repair semantics: [Open a document after a file corruption error](https://support.microsoft.com/en-us/word/open-a-document-after-a-file-corruption-error), [How to troubleshoot damaged documents in Word](https://learn.microsoft.com/en-us/previous-versions/troubleshoot/microsoft-365/microsoft-365-apps/word/damaged-documents-in-word)
- Open XML validation: [OpenXmlValidator](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.validation.openxmlvalidator)
- Formats/specs: [MS-DOCX](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/b839fe1f-e1ca-4fa6-8c26-5954d0abbccd), [MS-DOCX CT_CommentsEx](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/4add9f34-fdba-4324-a9d6-60e78897c5a6), [MS-DOCX Appendix A](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-docx/d0a2e301-0ff7-4e9e-9bb7-ff47070dce0a), [MS-OI29500](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oi29500/1fd4a662-8623-49c0-82f0-18fa91b413b8), [ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/), [WordprocessingPeoplePart (SDK)](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.packaging.wordprocessingpeoplepart)
- Threaded comments availability: [Collaborate with comments in Office 365](https://support.microsoft.com/en-us/office/collab-files/collaborate-with-comments-in-office-365)
- LibreOffice: [start_parameters help](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html), [convert filters](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html), [API](https://api.libreoffice.org/), [25.2 release notes](https://wiki.documentfoundation.org/ReleaseNotes/25.2), [licenses](https://www.libreoffice.org/licenses/), [unoserver](https://github.com/unoconv/unoserver)
- System requirements / deployment / licensing: [M365 system requirements](https://support.microsoft.com/en-us/office/system-requirements/system-requirements-for-microsoft-365-for-business-education-and-government-use), [Office for Mac deployment guide](https://learn.microsoft.com/en-us/microsoft-365-apps/mac/deployment-guide-for-office-for-mac), [Windows Server support for M365 Apps](https://learn.microsoft.com/en-us/microsoft-365-apps/end-of-support/windows-server-support), [shared computer activation](https://learn.microsoft.com/en-us/microsoft-365-apps/licensing-activation/overview-shared-computer-activation)
- Runners/cloud: [GitHub-hosted runners](https://docs.github.com/en/actions/using-github-hosted-runners/about-github-hosted-runners), [Graph convert endpoint](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content-format)
- Fixture licensing: [python-docx LICENSE](https://github.com/python-openxml/python-docx/blob/master/LICENSE), [Open-XML-SDK LICENSE](https://github.com/OfficeDev/Open-XML-SDK/blob/main/LICENSE), [LibreOffice licenses](https://www.libreoffice.org/licenses/), [Apache POI](https://poi.apache.org/)
- Repo-internal anchors: `verification.md` (LibreOffice interop gate), `docs/adr/0024-real-docx-fixture-corpus.md`, `corpus/.gitignore` (`real/` private tier), `.github/workflows/release-qualification.yml` (ubuntu LibreOffice install), `scripts/release_acceptance.py` (soffice convert check), commit 34b98f1 (rsid-normalized fixture hash gate)
