# 0028 — Expose vertical-format candidates before conversion

Normalization must first produce an agent-readable candidate report containing paragraph/occurrence IDs, code points, Unicode names, category, proposed mapping, and local text context. The agent writes explicit convert/preserve decisions to a policy and audit log; normal extraction and build never infer semantic intent or mutate a candidate implicitly.
