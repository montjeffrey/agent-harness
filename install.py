"""Source-checkout shim: `python install.py ...` re-exports `harness.install_cli`.

The installer's real implementation lives in `harness/install_cli.py` (also
shipped as the `agent-harness-install` console script). This file exists
only so a git clone's `./install.py` / `setup.sh` / `setup.ps1` entry points
keep working unchanged.
"""

from __future__ import annotations

from harness.install_cli import (  # noqa: F401
    CLAUDE_POINTER,
    GEMINI_POINTER,
    GITIGNORE_ENTRY,
    HARNESS_ROOT,
    HOOK_FRAGMENT_PATH,
    MARKER_END,
    MARKER_START,
    MCP_SERVER_NAME,
    VAULT_README_TEMPLATE,
    InstallError,
    InstallTransaction,
    _json_string_escape,
    _shell_quote,
    deep_merge_hooks,
    ensure_gitignore_entry,
    install_hooks,
    install_mcp_registration,
    install_vault_files,
    main,
    parse_args,
    render_agents_block,
    render_vault_readme,
    run_install,
    upsert_marker_block,
)

if __name__ == "__main__":
    raise SystemExit(main())
