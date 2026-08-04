"""verify: compare two DOCX paragraph-by-paragraph at the XML level.

Normalization: rsid attributes stripped; attribute order sorted. Every
paragraph must match exactly, otherwise exit non-zero with a diff report.
"""
import argparse
import re
import zipfile


def normalize(xml_fragment):
    x = re.sub(r'w:rsid[A-Za-z]*="[^"]*"', '', xml_fragment)
    # <w:r > / <w:r  > (trailing space from Word) == <w:r> — normalize
    x = re.sub(r'<w:r\s+>', '<w:r>', x)
    x = re.sub(r'<w:p\s+>', '<w:p>', x)

    def sort_attrs(m):
        tag = m.group(0)
        inner = tag[1:-1].strip()
        if not inner:
            return tag
        sp = inner.find(' ')
        if sp < 0:
            return tag
        name, attrs = inner[:sp], inner[sp + 1:]
        attrs_list = re.findall(r'([\w:]+="[^"]*")', attrs)
        attrs_list.sort()
        return '<' + name + ' ' + ' '.join(attrs_list) + '>'

    return re.sub(r'<[^>]+>', sort_attrs, x)


def extract_paragraphs(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    paras = re.findall(r'<w:p[ >].*?</w:p>', xml, re.DOTALL)
    return [normalize(p) for p in paras]


def verify(argv):
    ap = argparse.ArgumentParser(prog='docx2typed verify')
    ap.add_argument('orig', help='original .docx')
    ap.add_argument('built', help='rebuilt .docx')
    args = ap.parse_args(argv)

    a = extract_paragraphs(args.orig)
    b = extract_paragraphs(args.built)
    same = 0
    diffs = []
    for i in range(max(len(a), len(b))):
        if i >= len(a) or i >= len(b):
            diffs.append(i)
            print(f'P{i}: paragraph count mismatch (orig={len(a)} built={len(b)})')
            continue
        if a[i] == b[i]:
            same += 1
        else:
            diffs.append(i)
            la, lb = len(a[i]), len(b[i])
            n = min(la, lb)
            f = next((k for k in range(n) if a[i][k] != b[i][k]), n)
            print(f'P{i}: diff at {f} (orig {la} chars vs built {lb} chars)')
            print(f'  orig: ...{a[i][max(0, f - 60):f + 60]}...')
            print(f'  built:...{b[i][max(0, f - 60):f + 60]}...')
    print(f'identical: {same}/{max(len(a), len(b))}')
    return 0 if not diffs else 1
