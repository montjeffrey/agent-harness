"""Content-level tests for the governance/approval wording surfaces.

Asserts:
(a) the generated blueprint carries exactly one approval signal (a `Status:`
    field) and no separate checkbox that could disagree with it.
(b) no generated text claims the approval gate is technically enforced or
    blocking -- the harness only documents an advisory convention.
(c) the blueprint template, the installed AGENTS instruction block, the
    generated vault README, and the repo root README all describe the same
    approval flow.
"""

from __future__ import annotations

import re
from pathlib import Path

import install

HARNESS_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = HARNESS_ROOT / "templates"
ROOT_README = HARNESS_ROOT / "README.md"

# Phrases that would imply the gate is a technical control rather than a
# human convention. Matched case-insensitively as substrings.
ENFORCEMENT_CLAIMS = [
    "tick the box",
    "tick the approved",
    "ticks the approved",
    "(or ticks",
]


def _blueprint_template_text() -> str:
    return (TEMPLATES_DIR / "feature-blueprint.md").read_text(encoding="utf-8")


def _agents_block_text() -> str:
    return install.render_agents_block(HARNESS_ROOT)


def _vault_readme_text() -> str:
    return install.render_vault_readme(HARNESS_ROOT)


def _root_readme_text() -> str:
    return ROOT_README.read_text(encoding="utf-8")


def test_blueprint_template_has_exactly_one_approval_signal() -> None:
    content = _blueprint_template_text()

    status_lines = [line for line in content.splitlines() if "**Status:**" in line]
    assert len(status_lines) == 1, f"expected exactly one Status field, found {status_lines}"

    # No second/competing approval signal (an "Approved" checkbox).
    assert not re.search(r"- \[ \] \*?\*?Approved\*?\*?", content), (
        "blueprint template must not carry a separate Approved checkbox "
        "alongside the Status field"
    )


def test_generated_blueprint_via_install_has_exactly_one_approval_signal(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    install.run_install(project, ["claude"], install_hooks_flag=False)

    generated = (project / "docs" / "vault" / "templates" / "feature-blueprint.md").read_text(
        encoding="utf-8"
    )

    status_lines = [line for line in generated.splitlines() if "**Status:**" in line]
    assert len(status_lines) == 1

    assert not re.search(r"- \[ \] \*?\*?Approved\*?\*?", generated)


def test_no_surface_claims_the_gate_is_enforced_or_blocking() -> None:
    surfaces = {
        "feature-blueprint.md": _blueprint_template_text(),
        "AGENTS.harness.md": _agents_block_text(),
        "docs/vault/README.md": _vault_readme_text(),
        "README.md": _root_readme_text(),
    }

    for name, text in surfaces.items():
        lowered = text.lower()
        for phrase in ENFORCEMENT_CLAIMS:
            assert phrase not in lowered, f"{name} still contains enforcement-implying phrase: {phrase!r}"


def test_agents_block_and_vault_readme_and_root_readme_state_advisory_framing() -> None:
    for name, text in (
        ("AGENTS.harness.md", _agents_block_text()),
        ("docs/vault/README.md", _vault_readme_text()),
        ("README.md", _root_readme_text()),
    ):
        assert "advisory" in text.lower(), f"{name} must state the gate is advisory"


def test_all_surfaces_agree_approval_is_set_via_status_field_only() -> None:
    """Every surface that describes how a human approves must reference the
    Status field, and none may describe a second/checkbox mechanism."""

    surfaces = {
        "feature-blueprint.md": _blueprint_template_text(),
        "AGENTS.harness.md": _agents_block_text(),
        "docs/vault/README.md": _vault_readme_text(),
        "README.md": _root_readme_text(),
    }

    for name, text in surfaces.items():
        assert "Status:" in text, f"{name} must reference the Status field as the approval signal"
        assert "Approved" in text, f"{name} must reference the Approved value"
        # None of these surfaces should reference ticking/checking a box as
        # part of the approval mechanism.
        assert "tick" not in text.lower(), f"{name} must not describe a checkbox-ticking approval step"
