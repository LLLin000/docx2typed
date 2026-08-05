# 0007 — Require explicit paragraph inheritance for inserted paragraphs

A new typed paragraph has no original XML record, so it must declare `inherit="P11"` and clone paragraph properties and base style from that existing paragraph. Implicit previous-paragraph inheritance and inline `pPr` XML were rejected because headings, numbering, and section boundaries make them unsafe and because inline XML would reopen format editing.
