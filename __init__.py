"""docx2typed — DOCX ⇄ editable-markdown with locked formatting.

Contract: the user edits ONLY text in the .md. Every paragraph's XML
(pPr, run rPr, inter-run elements, rsid) lives in the .format.json and
is replayed verbatim on build. Round-trip must be byte-identical at the
paragraph-XML level (modulo rsid, which is stripped by default).

CLI:
  python -m docx2typed extract <input.docx> -o <outdir>
  python -m docx2typed view <input.md> [-o out.txt]
  python -m docx2typed build <input.md> <format.json> -o <output.docx>
  python -m docx2typed verify <orig.docx> <built.docx>
"""
import sys

from .extract import extract
from .view import view
from .build import build
from .verify import verify


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == 'extract':
        return extract(argv[1:])
    if cmd == 'view':
        return view(argv[1:])
    if cmd == 'build':
        return build(argv[1:])
    if cmd == 'verify':
        return verify(argv[1:])
    print(f'Unknown command: {cmd}\n{__doc__}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
