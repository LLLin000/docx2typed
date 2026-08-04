"""extract: DOCX → <name>.md + <name>.format.json

The .md holds ONLY text (one line per run, `[n] ` prefix) plus
`<!-- XML:... -->` placeholders for inter-run elements. All formatting
XML goes to the .format.json. Empty paragraphs get a default run so the
user can fill text in later.
"""
import argparse
import json
import os
import re
import shutil
import zipfile


def parse_paragraphs(docx_path):
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    return re.findall(r'<w:p[ >].*?</w:p>', xml, re.DOTALL)


def split_paragraph(p):
    """Return (attrs_str, ppr_xml, items) where items = list of
    ('elem', xml) or ('run', run_xml)."""
    m = re.match(r'<w:p\s([^>]*)>', p)
    attrs = m.group(1).strip() if m else ''
    ppr = re.search(r'<w:pPr>.*?</w:pPr>', p, re.DOTALL)
    ppr_xml = ppr.group(0) if ppr else ''
    body = p[len(m.group(0)):len(p) - 6] if m else p
    if ppr_xml:
        body = body.replace(ppr_xml, '', 1)
    items = []
    pos = 0
    for rm in re.finditer(r'<w:r(?:\s[^>]*)?>.*?</w:r>', body, re.DOTALL):
        if rm.start() > pos:
            items.append(('elem', body[pos:rm.start()]))
        items.append(('run', rm.group(0)))
        pos = rm.end()
    if pos < len(body):
        items.append(('elem', body[pos:]))
    return attrs, ppr_xml, items


def parse_run(run_xml):
    """Extract (open_attrs, rpr_xml, mid_xml, text, ws_flag)."""
    m = re.match(r'<w:r\s([^>]*)>', run_xml)
    open_attrs = m.group(1).strip() if m else ''
    rpr = re.search(r'<w:rPr>.*?</w:rPr>', run_xml, re.DOTALL)
    rpr_x = rpr.group(0) if rpr else ''
    mid = re.sub(r'<w:rPr>.*?</w:rPr>', '', run_xml, flags=re.DOTALL)
    mid = re.sub(r'<w:t[^>]*>.*?</w:t>', '', mid, flags=re.DOTALL)
    mid = re.sub(r'</?w:r[^>]*>', '', mid, flags=re.DOTALL)
    mid = ' '.join(mid.split())
    ts = re.findall(r'<w:t([^>]*)>([^<]*)</w:t>', run_xml)
    text = ''.join(t[1] for t in ts)
    ws = any('xml:space' in t[0] for t in ts)
    return open_attrs, rpr_x, mid, text, ws


def extract(argv):
    ap = argparse.ArgumentParser(prog='docx2typed extract')
    ap.add_argument('input', help='input .docx')
    ap.add_argument('-o', '--outdir', default='.', help='output directory')
    ap.add_argument('--keep-rsid', action='store_true',
                    help='keep rsid attributes (default: strip)')
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        print(f'ERROR: file not found: {args.input}')
        return 1
    try:
        paragraphs = parse_paragraphs(args.input)
    except zipfile.BadZipFile:
        print(f'ERROR: not a valid .docx (zip) file: {args.input}')
        return 1
    except Exception as e:
        print(f'ERROR: cannot read {args.input}: {e}')
        return 1

    name = os.path.splitext(os.path.basename(args.input))[0]
    os.makedirs(args.outdir, exist_ok=True)
    md_path = os.path.join(args.outdir, name + '.md')
    json_path = os.path.join(args.outdir, name + '.format.json')
    tpl_path = os.path.join(args.outdir, '_template.docx')

    paragraphs = parse_paragraphs(args.input)
    data = {'paragraphs': []}
    md_lines = [
        '<!-- meta',
        f'format: {os.path.basename(json_path)}',
        f'template: {os.path.basename(tpl_path)}',
        f'source: {os.path.basename(args.input)}',
        f'paragraphs: {len(paragraphs)}',
        '-->',
        '',
    ]

    for pi, p in enumerate(paragraphs):
        attrs, ppr_x, items = split_paragraph(p)
        # strip rsid unless asked to keep
        if not args.keep_rsid:
            attrs = re.sub(r'w:rsid[A-Za-z]*="[^"]*"', '', attrs).strip()
        para_rec = {'attrs': attrs, 'ppr': ppr_x, 'items': []}
        md_lines.append(f'<!-- P{pi} -->')
        run_no = 0
        for kind, content in items:
            if kind == 'elem':
                xml_part = ' '.join(content.split())
                if xml_part:
                    para_rec['items'].append({'t': 'elem', 'xml': xml_part})
                    md_lines.append(f'<!-- XML:{xml_part} -->')
            else:
                open_a, rpr_x, mid_x, text, ws = parse_run(content)
                if not args.keep_rsid:
                    open_a = re.sub(r'w:rsid[A-Za-z]*="[^"]*"', '', open_a).strip()
                para_rec['items'].append({
                    't': 'run', 'open': open_a, 'rpr': rpr_x,
                    'mid': mid_x, 'ws': ws,
                })
                run_no += 1
                md_lines.append(f'[{run_no}] {text}')
        if not para_rec['items']:
            # empty paragraph: no run, no [n] slot. User may add text later
            # by editing this paragraph's empty line in the .md.
            para_rec['empty'] = True
        md_lines.append('')
        data['paragraphs'].append(para_rec)

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(md_lines))
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    shutil.copy2(args.input, tpl_path)
    print(f'extracted: {md_path}')
    print(f'format:    {json_path}')
    print(f'template:  {tpl_path} ({len(paragraphs)} paragraphs)')
    return 0
