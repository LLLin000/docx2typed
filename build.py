"""build: <name>.md + <name>.format.json → DOCX (verbatim replay)."""
import argparse
import json
import os
import re
import shutil

from docx import Document
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml

NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W = qn


def parse_blocks(md_text):
    lines = md_text.split('\n')
    blocks = []
    cur = None
    for ln in lines:
        if ln.startswith('<!-- P'):
            if cur:
                blocks.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def build(argv):
    ap = argparse.ArgumentParser(prog='docx2typed build')
    ap.add_argument('input', help='input .md')
    ap.add_argument('format', help='.format.json')
    ap.add_argument('-o', '--output', help='output .docx (default: <md>.docx)')
    ap.add_argument('--template', help='template docx (default: from md meta)')
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        print(f'ERROR: file not found: {args.input}')
        return 1
    if not os.path.exists(args.format):
        print(f'ERROR: file not found: {args.format}')
        return 1
    with open(args.input, encoding='utf-8') as f:
        md_text = f.read()
    try:
        with open(args.format, encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f'ERROR: invalid format json: {e}')
        return 1
    paras_json = data.get('paragraphs', [])

    # meta / template resolution
    meta = re.search(r'<!-- meta\n(.*?)-->', md_text, re.DOTALL)
    tpl_name = None
    if meta:
        m = re.search(r'template:\s*(\S+)', meta.group(1))
        if m:
            tpl_name = m.group(1)
    template = args.template
    if not template:
        base = os.path.dirname(os.path.abspath(args.input))
        template = os.path.join(base, tpl_name) if tpl_name else None
    if not template or not os.path.exists(template):
        print(f'ERROR: template not found ({template}). Use --template.')
        return 1

    output = args.output or os.path.splitext(args.input)[0] + '.docx'
    shutil.copy2(template, output)
    doc = Document(output)
    body = doc.element.body
    for p in list(body.findall(f'{{{NS}}}p')):
        body.remove(p)

    blocks = parse_blocks(md_text)
    md_paras = [b for b in blocks if b[0].startswith('<!-- P') and '<!-- XML:' not in b[0]]
    if len(md_paras) != len(paras_json):
        print(f'ERROR: paragraph count mismatch: md={len(md_paras)} json={len(paras_json)}')
        return 1

    for blk, rec in zip(md_paras, paras_json):
        header = blk[0]
        m = re.match(r'<!-- P(\d+) -->', header)
        if not m:
            continue
        pidx = int(m.group(1))
        p_xml = f'<w:p {nsdecls("w", "w14", "r")} {rec["attrs"]}>'
        if rec.get('ppr'):
            p_xml += rec['ppr']
        p_xml += '</w:p>'
        p_elem = parse_xml(p_xml)
        body.append(p_elem)

        items = rec.get('items', [])
        # collect md run texts in order + validate line numbers contiguous
        md_texts = []
        md_nums = []
        for line in blk[1:]:
            line = line.rstrip('\r\n')
            rm = re.match(r'\[(\d+)\]\s?(.*)$', line, re.DOTALL)
            if rm:
                md_nums.append(int(rm.group(1)))
                md_texts.append(rm.group(2))
        if md_nums != list(range(1, len(md_nums) + 1)):
            print(f'ERROR: P{pidx} run numbers not contiguous: {md_nums}')
            return 1
        # count expected run slots in json (runs only; elems interleave)
        json_run_count = sum(1 for it in items if it['t'] == 'run')

        # empty paragraph: no json runs; md may have 0 or 1 text slot
        if json_run_count == 0:
            if len(md_texts) > 1:
                print(f'ERROR: P{pidx} empty paragraph has {len(md_texts)} run lines (max 1)')
                return 1
            if md_texts and md_texts[0]:
                # fill: create a default-format run
                default_rpr = ''
                for prev in reversed(paras_json[:pidx]):
                    for it in prev.get('items', []):
                        if it['t'] == 'run' and it.get('rpr'):
                            default_rpr = it['rpr']
                            break
                    if default_rpr:
                        break
                if not default_rpr:
                    default_rpr = ('<w:rPr><w:rFonts w:ascii="Times New Roman" '
                                   'w:eastAsia="宋体" w:hAnsi="Times New Roman" '
                                   'w:cs="Times New Roman"/><w:sz w:val="24"/>'
                                   '<w:szCs w:val="24"/></w:rPr>')
                text = md_texts[0]
                t_attr = ' xml:space="preserve"' if (text and text != text.strip()) else ''
                r_xml = f'<w:r {nsdecls("w")}>{default_rpr}<w:t{t_attr}>{text}</w:t></w:r>'
                p_elem.append(parse_xml(r_xml))
            # emit any elems
            for it in items:
                if it['t'] == 'elem' and it.get('xml', '').strip():
                    wrap = parse_xml(f'<w:__w {nsdecls("w","w14","r")}>{it["xml"]}</w:__w>')
                    for ch in list(wrap):
                        p_elem.append(ch)
            continue

        if len(md_texts) != json_run_count:
            print(f'ERROR: P{pidx} run count mismatch: md={len(md_texts)} json={json_run_count}')
            return 1

        # json-driven replay: elems from json, text from md queue
        ti = 0
        for it in items:
            if it['t'] == 'elem':
                if it.get('xml', '').strip():
                    wrap = parse_xml(f'<w:__w {nsdecls("w","w14","r")}>{it["xml"]}</w:__w>')
                    for ch in list(wrap):
                        p_elem.append(ch)
            else:
                text = md_texts[ti]
                ti += 1
                t_attr = ' xml:space="preserve"' if it.get('ws') else ''
                t_part = f'<w:t{t_attr}>{text}</w:t>' if text else ''
                r_xml = (f'<w:r {nsdecls("w")} {it.get("open", "")}>'
                         f'{it.get("rpr", "")}{it.get("mid", "")}{t_part}</w:r>')
                p_elem.append(parse_xml(r_xml))

    doc.save(output)
    print(f'saved: {output} ({len(doc.paragraphs)} paragraphs)')
    return 0
