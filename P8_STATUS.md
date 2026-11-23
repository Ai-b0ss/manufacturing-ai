# P8 — title-block evidence, actual STEP topology and change-reference verification

Date: 17 November 2026.
Branch: feat/p8-drawing-revision-verification-20261117.
Workspace: C:\Users\user\Documents\ManufacturingAI-P8-Engineering-Verification

## Product behavior

P8 extends the P7 open-hardware corpus without modifying P7, the main demo,
or the original public engineering source files.

- A separate title-block extractor reads actual words/coordinates from 19 PDF
  source documents, with dedicated conservative patterns for Passport Assembly,
  BCN3D and ArcBlue. Metadata includes page number and bounding box.
- Passport: FD_1842300-H extracts actual drawing 1842300 / rev H; the PDF's
  visible revision table includes G (12/9/2020) and H (3/16/2021).
  Other portrait/landscape drawings are recognized independently of filename.
- BCN3D: extracts reference and the actual "Versió plànol" value, including
  040M07C01.0P026D / V1.0. A published filename and printed reference that
  disagree are marked MISMATCH, never silently normalized. The Sigmax/R19
  top-frame drawings have different part identifiers despite both displaying
  V1.0; this is NOT proof of a formal change notice.
- ArcBlue: an explicit 6061-T6 token matches the BOM where present; a drawing
  says AISI 304 while BOM says SUS304 for lower_square_tube. No automatic
  material-grade equivalence is asserted. Many ArcBlue revision fields cannot
  be reliably read; they remain UNKNOWN.
- OpenCascade reads actual STEP solid BREP topology (not approximations from
  STEP Cartesian-point text), checks BREP validity and reports solid count,
  face count, volume and axis-aligned bounding dimensions.
  Three real ArcBlue STEP models pass this narrow topology check.
- The historical PDF notice to СТО ЦКТИ 318.03-2009 is kept separate from
  public open-hardware files. Its identifier and document are detectable on
  page 13, but degree/tolerance glyphs are corrupted in the text layer, so the
  claimed angle change and effective date are NOT independently confirmed.
- Every P8 source/part report explicitly has manufacturing_authorization=false;
  no approved revision or PLM release record is invented.

## Provenance and security

- P8 is a local-only educational demo using role buttons, NOT enterprise SSO.
- P8 inspection index is data\engineering_inspection.sqlite3, separate from
  data\engineering_corpus.sqlite3 (P7) and excluded from Git.
- Source SHA-256 is checked again before serving any cached inspection; results
  are invalidated on changed/missing files and on P7 manifest mismatch.
- Inspections are created using bounded, short-lived subprocesses and a
  verified, immutable temporary file. They do NOT run in a hardened OS sandbox.
- All /api/engineering/p8/* routes require the engineering ACL; access is rechecked after parsing and before returning evidence, preventing revocation races. Technologist,
  anonymous and finance-only roles cannot probe source presence by direct ID.
- Limited OCR of a PDF title-block region is opt-in and marked
  UNVERIFIED_OCR_HINT; it can never change approved revision status.
- User inputs are never used as filesystem paths or shell commands.
- Historical copyrighted PDF is kept outside the public corpus; do not
  redistribute it. BCN3D drawings contain additional reproduction notices.

## Dependencies

Ordinary application uses installed Python 3.13 and local PyMuPDF through
the existing PDF worker. CAD geometry runs only in this project's isolated
.p8venv with cadquery-ocp==8.0.1.1.0 (OpenCascade, Apache-2.0).
The current venv also contains its pip-resolved VTK/dependencies.
No global Python or GPU package was changed.

To create an equivalent isolated CAD environment:

    C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe -m venv .p8venv
    .p8venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-cad.txt

To rebuild indices (both publish atomically on successful import):

    IMPORT_CORPUS.cmd

To start in a separate Windows console:

    START_DEMO.cmd

P8 is available only on the PC at http://127.0.0.1:19129/
OPEN_DEMO.cmd opens it; TEST_DEMO.cmd runs all unit/HTTP regressions.

For CLI:

    python engineering_corpus.py import
    python engineering_inspection.py import
    python engineering_inspection.py summary
    python engineering_inspection.py part --query focuser_drawtube
    python engineering_inspection.py compare-top
    python engineering_inspection.py notice
    python test_runner.py
    python p8_browser_harness.py

## Standards context, not a compliance claim

ГОСТ Р 2.104-2023 (title blocks) and ГОСТ Р 2.503-2023
(engineering change documents) both entered into effect in the Russian
Federation on 1 March 2024. Official source pages:
https://protect.gost.ru/gost/details/69d422a2-3c0e-4561-a521-0c99f9cf4417
https://protect.gost.ru/gost/details/407f65aa-afd7-466b-ac32-725e3654a138

The actual dataset contains non-ESKD drawings from international projects.
No ESKD conformity validation, electronic-signature verification, geometry
alignment between PDF and STEP, completeness of revisions, applicability to
lots, or PLM/1C integration exists in P8.

## Verified run on the owner Windows workstation — 17 November 2026

- Full combined suite passed: **Ran 138 tests in 59.399s; OK; exit 0**.
  This includes all previous 116 P7/P6 tests and 22 new P8 tests.
- Actual Windows OCR on the ArcBlue PDF title-block crop returned a candidate
  in 2.1 seconds, with OCR_REQUIRES_VISUAL_REVIEW and both
  revision_approved/manufacturing_authorization false.
- Chrome headless acceptance passed, including a Passport H stamp with G/H
  revision history, 3 actual P7 original downloads, P8 BREP result, BCN3D
  two-release identifiers, historical notice, and engineering ACL checks.
- Browser viewport sizes 1440, 1024, 768, 390 had scrollWidth equal to
  viewport/client width (no horizontal overflow), zero JavaScript exceptions.
  The initially detected mobile CSS overflow was corrected and re-tested.
- Real PDF/STEP metadata index: 40 source rows, 19 PDFs, 3 STEP models,
  14 PDFs with candidate fields, 3 OpenCascade valid BREP topologies.
- Historical corrupted degree/tolerance text and material-code disagreement
  remain explicitly unverified; both are intentionally NOT normalized.
- Access revocation during source inspection was simulated with a blocked
  worker and a separate session: the response was HTTP 409, and no source
  details were returned.
- Browser evidence: data/p8_browser_report.json;
  data/p8_engineering_desktop.jpg and data/p8_engineering_mobile.jpg.

## Acceptance

Full integration suite: run python test_runner.py; includes P7 regressions
and 22 new P8 tests. Browser acceptance uses isolated headless Chrome profiles,
tests engineering ACL, an actual PDF stamp, an actual STEP model, two release
drawings, the real historical notice and responsive widths.

A model with valid topology does NOT mean it matches a drawing. A visible
revision field does NOT confirm an approved/current revision. Manufacturing
authorization remains forbidden throughout P8.
