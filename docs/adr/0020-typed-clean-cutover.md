# 0020 — Make typed mode the sole format on the experiment branch

The experiment does not preserve v1 strict-run compatibility. `extract`, `build`, `view`, `validate`, and `verify` use the typed schema directly; old `[n]` workdirs are not migrated from their Markdown. Re-generation must start from the original DOCX/template so style, XML, relationship, and package baselines are available; if the original DOCX is missing, the tool fails rather than inferring them.
