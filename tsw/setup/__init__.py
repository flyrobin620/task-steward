"""The install script ``tsw setup`` (DN009, CC009).

Both install paths, the terminal entry ``setup.ps1`` and the session entry (the plugin's setup
skill), only ask the questions and hand the answers to this package. It owns the deterministic
steps: ``check`` (read-only, with the question list), ``plan`` (read-only: the actions and the
user-settings diff), ``install`` (step by step, recording ownership in ``install.json`` before
each change), ``verify`` (two phases) and ``uninstall``.

Modules: :mod:`.record` (``install.json`` and the answers file), :mod:`.questions` (the one
question list), :mod:`.winenv` (the ``tsw`` shims and the user-level ``Path``), :mod:`.steps`
(the steps themselves). The host's part (registering the plugin source, the user settings) is
the host integration's, :mod:`tsw.host.claude_install`.

Only the command line imports this package; the MCP server never does (``update-plan`` counts a
change here as ``install``, not as a runtime change that needs a session reload).
"""
