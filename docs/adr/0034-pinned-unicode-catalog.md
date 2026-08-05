# 0034 — Pin and commit the Unicode vertical catalog

The generated catalog is tied to a declared Unicode data version and committed with its hash; runtime reads it rather than deriving candidates from the host Python Unicode database. A Unicode data upgrade is an explicit catalog migration with fixtures and audit-version changes.
