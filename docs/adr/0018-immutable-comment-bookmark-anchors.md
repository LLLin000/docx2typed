# 0018 — Keep comment and bookmark anchors immutable

Existing comment/bookmark ranges remain paired, ordered, and identified by their original IDs/names; text inside a range may change, but v2.0 cannot add, remove, or move the anchors or edit `comments.xml` metadata. Empty ranges are retained when their text is deleted.
