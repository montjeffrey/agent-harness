"""Strict JSON/config validation shared by install.py and harness/doctor.py.

Both the installer and the preflight doctor need to read the same two kinds
of harness-managed JSON files -- a project's `.claude/settings.json` and the
operator-level `~/.agent-harness/config.json`. Historically each read its
own copy with slightly different error handling, which risked one of them
treating malformed JSON as an empty object and silently merging into it.

This module is the single source of truth: malformed JSON (syntax errors, or
valid JSON that isn't a top-level object) always raises `ConfigValidationError`
and is never treated as `{}`.
"""

from __future__ import annotations

import json
from pathlib import Path


class ConfigValidationError(ValueError):
    """A harness-managed JSON config file is missing, unreadable, or malformed."""


def parse_json_object(text: str, *, source: str) -> dict:
    """Strictly parse `text` as a JSON object.

    Raises `ConfigValidationError` if the text is not valid JSON, or if it
    parses to something other than a top-level JSON object (e.g. a list or
    a scalar). Never returns a partial/best-effort result.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"{source}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigValidationError(
            f"{source}: expected a JSON object at the top level, got {type(data).__name__}"
        )
    return data


def load_json_object(path: Path) -> dict:
    """Read and strictly parse a JSON object file.

    Raises `ConfigValidationError` if the file cannot be read or does not
    contain a valid top-level JSON object. Callers that want to treat a
    missing file as "no config yet" must check `path.exists()` themselves
    before calling this -- a missing file is still an error here.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigValidationError(f"{path}: unreadable ({exc})") from exc
    return parse_json_object(text, source=str(path))
