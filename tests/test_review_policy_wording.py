"""Content-level tests for the review policy carried by the instruction block.

The policy asks agents to plan work as small, single-purpose pull requests,
each with its testing evidence. It only works if an agent understands why, so
these tests pin the reason alongside the rules:

(a) the installed AGENTS block has a Review policy section that states the
    reviewer-time reason, slices work before code, caps a PR's size, requires
    testing evidence and a failing test before a fix, and forbids stacking.
(b) the section claims no enforcement -- no gate checks PR size yet, so the
    wording must not say one does.
(c) the feature blueprint, where planning happens, carries a PR Slices
    section, and so does the copy an install generates.
"""

from __future__ import annotations

import re
from pathlib import Path

import install

HARNESS_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = HARNESS_ROOT / "templates"

# Wording that would claim a technical control which does not exist yet.
ENFORCEMENT_CLAIMS = ["is blocked", "are blocked", "enforced", "pre-push", "refuses", "rejects"]


def _review_policy_section() -> str:
    block = install.render_agents_block(HARNESS_ROOT)
    match = re.search(r"^### Review policy\n(.*?)(?=^### |<!-- agent-harness:end -->)", block, re.M | re.S)
    assert match, "AGENTS block must carry a '### Review policy' section"
    return match.group(1)


def _slices_section(blueprint: str) -> str:
    match = re.search(r"^## PR Slices\n(.*?)(?=^## )", blueprint, re.M | re.S)
    assert match, "blueprint must carry a '## PR Slices' section before the next section"
    return match.group(1)


def test_review_policy_states_the_reviewer_time_reason() -> None:
    lowered = _review_policy_section().lower()
    assert "reviewer" in lowered
    assert "time" in lowered


def test_review_policy_slices_work_before_code_and_caps_size() -> None:
    lowered = _review_policy_section().lower()
    assert "before writing code" in lowered
    assert "slice" in lowered
    assert re.search(r"\b500 changed lines\b", lowered), "the size guideline must name a concrete line count"


def test_review_policy_requires_testing_evidence_and_failing_test_first() -> None:
    section = _review_policy_section()
    assert "Testing" in section, "PR bodies must carry a Testing section"
    lowered = section.lower()
    assert "commands" in lowered
    assert "failing test" in lowered


def test_review_policy_forbids_stacking_on_another_open_pr() -> None:
    lowered = _review_policy_section().lower()
    assert "stack" in lowered
    assert "another open pr" in lowered


def test_review_policy_claims_no_enforcement() -> None:
    lowered = _review_policy_section().lower()
    for phrase in ENFORCEMENT_CLAIMS:
        assert phrase not in lowered, f"review policy implies a gate that does not exist: {phrase!r}"
    assert "advisory" in lowered


def test_blueprint_template_plans_pr_slices_before_execution() -> None:
    blueprint = (TEMPLATES_DIR / "feature-blueprint.md").read_text(encoding="utf-8")
    slices = _slices_section(blueprint).lower()
    for field in ("change", "tests", "estimated lines"):
        assert field in slices, f"PR Slices must ask for {field!r} per slice"
    assert blueprint.index("## PR Slices") < blueprint.index("## Execution Log")


def test_generated_blueprint_via_install_carries_pr_slices(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    install.run_install(project, ["claude"], install_hooks_flag=False)

    generated = (project / "docs" / "vault" / "templates" / "feature-blueprint.md").read_text(
        encoding="utf-8"
    )
    _slices_section(generated)
