"""Tests for the shared strict JSON/config validation used by doctor.py and
install.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import config_validation
from harness.config_validation import ConfigValidationError


def test_parse_json_object_accepts_valid_object() -> None:
    data = config_validation.parse_json_object('{"a": 1}', source="test")
    assert data == {"a": 1}


def test_parse_json_object_rejects_malformed_json() -> None:
    with pytest.raises(ConfigValidationError) as exc_info:
        config_validation.parse_json_object("{not json", source="settings.json")
    assert "settings.json" in str(exc_info.value)


def test_parse_json_object_rejects_non_object_top_level() -> None:
    with pytest.raises(ConfigValidationError):
        config_validation.parse_json_object("[1, 2, 3]", source="settings.json")

    with pytest.raises(ConfigValidationError):
        config_validation.parse_json_object('"just a string"', source="settings.json")


def test_load_json_object_reads_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert config_validation.load_json_object(path) == {"hooks": {}}


def test_load_json_object_raises_on_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        config_validation.load_json_object(path)


def test_load_json_object_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError):
        config_validation.load_json_object(tmp_path / "does-not-exist.json")


def test_load_json_object_never_returns_empty_dict_for_malformed_input(tmp_path: Path) -> None:
    """Regression guard: malformed JSON must never be silently treated as {}."""
    path = tmp_path / "config.json"
    path.write_text("not json at all", encoding="utf-8")
    try:
        result = config_validation.load_json_object(path)
    except ConfigValidationError:
        result = None
    assert result is None
