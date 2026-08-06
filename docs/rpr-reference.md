# rPr 翻译字典（rPr translation dictionary）

How to read a Word run-properties fragment (`<w:rPr>...</w:rPr>`) as plain
language. Every character style in a typed workdir is identified by a
content-addressed `style_id` (`s_<sha256>`): equal `style_id` means the rPr is
byte-equivalent; different `style_id` means different formatting, whatever the
labels say. Use this table to translate the canonical rPr XML stored in
`styles.json` (per style) or the `rpr` field of a region.

## Fonts

| XML | Meaning |
|---|---|
| `<w:rFonts w:ascii="Times New Roman"/>` | Western/Latin font |
| `<w:rFonts w:eastAsia="宋体"/>` | East-Asian (CJK) font |
| `<w:rFonts w:hAnsi="Calibri"/>` | High-ANSI font |
| `<w:rFonts w:cs="Arial"/>` | Complex-script font |
| `<w:rFonts w:hint="eastAsia"/>` | Hint which font slot to use at caret |

A single run often carries several slots — `ascii=Times New Roman` +
`eastAsia=宋体` is the normal Chinese/English mixed setting: the same style
renders 中文 in 宋体 and ABC in Times New Roman.

## Character properties

| XML | Meaning |
|---|---|
| `<w:b/>` / `<w:b w:val="1"/>` | bold |
| `<w:i/>` | italic |
| `<w:u w:val="single"/>` | underline (single/double/wave…) |
| `<w:strike/>` | strikethrough |
| `<w:dstrike/>` | double strikethrough |
| `<w:vertAlign w:val="superscript"/>` / `"subscript"` | superscript / subscript |
| `<w:position w:val="N"/>` | raised/lowered by N half-points |
| `<w:color w:val="FF0000"/>` | font color as RRGGBB |
| `<w:sz w:val="24"/>` | font size in half-points (24 = 12 pt) |
| `<w:szCs w:val="24"/>` | complex-script size |
| `<w:kern w:val="2"/>` | kerning (half-points) |
| `<w:spacing w:val="20"/>` | character spacing (twentieths of a point) |
| `<w:w w:val="100"/>` | character width percentage |
| `<w:highlight w:val="yellow"/>` | highlight color |
| `<w:smallCaps/>` | small caps |
| `<w:caps/>` | all caps |
| `<w:outline/>` | outlined text |
| `<w:imprint/>` | embossed |
| `<w:em w:val="dot"/>` | emphasis mark |
| `<w:textEffect w:val="blinkBackground"/>` | text effect |
| `<w:lang w:val="en-US"/>` | language |
| `<w:rtl/>` / `<w:cs/>` | right-to-left / complex-script layout |
| `<w:rStyle w:val="Heading1Char"/>` | named character-style reference |

## Reading order

1. Find the style by `style_id` in `styles.json` → `rpr` field.
2. Translate each child element with this table.
3. If an element is missing from this table, it is rare — treat the region as
   "style differs, details in rpr" rather than guessing.
