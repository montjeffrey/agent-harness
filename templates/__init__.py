"""Packaging marker: makes `templates/` an importable package so its
resource files (AGENTS.harness.md, feature-blueprint.md) ship in the wheel
and are resolvable post-install via importlib.resources."""
