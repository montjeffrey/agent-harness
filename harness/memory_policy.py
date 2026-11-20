"""The operator's acceptance policy for agent-recorded memory.

Two policies:

    review   (default) an agent's conclusion stays `proposed` until a human
             accepts it with ``agent-harness-memory review``.
    auto     curation accepts agent-recorded proposals on its own, in stores
             this machine's operator enrolled.

The setting is one key in ``~/.agent-harness/config.json``::

    {"memory_acceptance": "auto"}

It is read from the home directory and nowhere else, on purpose. A project
tree arrives with a clone and can hold any text; if a repository could carry
this switch, cloning it would be enough to make its shipped memory accepted.
A repository cannot write the home directory.

That is the whole of the guarantee, and it is narrower than it may sound. A
process running as the operator -- including an agent given file-write or
shell permission -- can write this file, and a session started with a
redirected home reads a different one. Neither is new: the same process could
already write accepted lines into the inbox directly. The policy does not
defend against an agent the operator has already let write anywhere.

Reading fails closed. A missing file, unreadable file, malformed JSON, or any
value other than the exact string ``"auto"`` means ``review``: the permissive
policy is something the operator has to have typed, never a fallback.

Stdlib-only and no heavier than one small file read, because curation runs
before every context retrieval.
"""

from __future__ import annotations

from pathlib import Path

from harness.config_validation import ConfigValidationError, load_json_object

ACCEPTANCE_KEY = "memory_acceptance"

REVIEW = "review"
AUTO = "auto"
POLICIES: tuple[str, ...] = (REVIEW, AUTO)


def config_path() -> Path:
    """The operator-level config file. Resolved per call so a changed home
    (tests, a different user) is honoured."""
    return Path.home() / ".agent-harness" / "config.json"


def acceptance_policy() -> str:
    """Return ``"auto"`` only when the operator config says exactly that."""
    path = config_path()
    if not path.is_file():
        return REVIEW
    try:
        config = load_json_object(path)
    except ConfigValidationError:
        return REVIEW
    value = config.get(ACCEPTANCE_KEY)
    return AUTO if isinstance(value, str) and value == AUTO else REVIEW
