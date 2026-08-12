"""Semantic-extra guidance must be valid for this unpublished checkout."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES_WITH_SEMANTIC_GUIDANCE = (
    "README.md",
    "harness/doctor.py",
    "harness/graph_index.py",
    "harness/graph_query.py",
    "harness/graph_refresh.py",
    "harness/memory.py",
    "harness/memory_index.py",
)


def test_semantic_guidance_uses_the_local_checkout_not_an_unpublished_pypi_name() -> None:
    for relative_path in FILES_WITH_SEMANTIC_GUIDANCE:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert 'pip install "agent-harness-kit[semantic]"' not in content, relative_path

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 'python -m pip install -e ".[semantic]"' in readme
    assert 'python -m pipx install --force "/path/to/agent-harness-kit[semantic]"' in readme
