# 0003 — Keep the v2.0 style registry immutable

Typed mode v2.0 is text-only. `styles.json`, `data-s` assignments, and structural tokens are generated from the source DOCX and are not an editing surface; changing them would be a format-mutation feature with separate inheritance, style-creation, and verification rules. Content-addressed style IDs keep references stable while that feature is deferred.
