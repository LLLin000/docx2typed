"""docx2typed view — join run-level md into readable paragraphs.

Reads a docx2typed .md and prints the document with each paragraph's
run texts joined into a continuous line, paragraph-per-block, so the
article content can be read without the format noise.

CLI:
  python -m docx2typed view <input.md>
"""
import argparse
import re
import sys

RUN_RE = re.compile(r'\[(\d+)\]\s?(.*)$', re.DOTALL)


def view(argv):
    ap = argparse.ArgumentParser(prog='docx2typed view')
    ap.add_argument('input', help='input .md (docx2typed extract output)')
    ap.add_argument('-o', '--output', help='write joined text to file instead of stdout')
    ap.add_argument('--no-paragraph-markers', action='store_true',
                    help='print plain paragraphs without "--- Pn ---" separators')
    args = ap.parse_args(argv)

    try:
        with open(args.input, encoding='utf-8') as f:
            md_text = f.read()
    except FileNotFoundError:
        print(f'ERROR: file not found: {args.input}')
        return 1

    lines = md_text.split('\n')
    paras = []
    cur = None
    for ln in lines:
        if ln.startswith('<!-- P'):
            if cur is not None:
                paras.append(cur)
            cur = [ln]
        elif cur is not None:
            rm = RUN_RE.match(ln.rstrip('\r\n'))
            if rm:
                cur.append(rm.group(2))
    if cur is not None:
        paras.append(cur)

    out_lines = []
    for blk in paras:
        if not args.no_paragraph_markers:
            m = re.match(r'<!-- (P\d+) -->', blk[0])
            out_lines.append(f'--- {m.group(1)} ---' if m else f'--- {blk[0]} ---')
        text = ''.join(blk[1:]).strip()
        if text:
            out_lines.append(text)
        out_lines.append('')

    result = '\n'.join(out_lines)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(result)
        print(f'view: wrote {args.output} ({len(paras)} paragraphs)')
    else:
        sys.stdout.write(result)
    return 0
