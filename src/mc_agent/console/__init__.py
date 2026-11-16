"""Standalone local Codex-style Web Console (`mc-console`) for `mc-harness`."""

from .server import ConsoleConfig, ConsoleController, create_console_app, main

__all__ = [
    "ConsoleConfig",
    "ConsoleController",
    "create_console_app",
    "main",
]
