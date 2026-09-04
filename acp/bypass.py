"""Bypass mode: `.acp/state/bypass`.

A flag file, not a config value -- presence alone puts ACP into bypass
mode, absence takes it out. An operator toggles it directly (touch/rm)
without restarting the service; `Compressor.compress()` checks it
fresh on every call (see compressor.py), before `gate.evaluate()` is
even reached, so the effect is immediate in both directions and needs
no code deploy to flip.

Reuses `acp/containment.py::resolve_root()` (`ACP_HOME` env var if
set, else the caller's own `root`, else cwd) rather than inventing a
second root-resolution convention. Mirrors AALP's own
`aalp/maintenance.py` (`.aalp/state/maintenance`) so an operator can
toggle either service the same way, independently.
"""
from __future__ import annotations

from pathlib import Path

from . import containment


def bypass_flag_path(root: str | Path | None = None) -> Path:
    return containment.resolve_root(root) / ".acp" / "state" / "bypass"


def is_bypass_mode(root: str | Path | None = None) -> bool:
    return bypass_flag_path(root).exists()


def enter_bypass(root: str | Path | None = None) -> None:
    """Create the flag file (and its parent dir) if not already present."""
    path = bypass_flag_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def exit_bypass(root: str | Path | None = None) -> None:
    """Remove the flag file if present; a no-op if already absent."""
    try:
        bypass_flag_path(root).unlink()
    except FileNotFoundError:
        pass
