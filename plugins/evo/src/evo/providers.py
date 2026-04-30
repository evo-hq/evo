"""Provider-side local install helpers."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def detect_install_method(executable: str | Path | None = None) -> str:
    """Best-effort classifier for how the evo CLI was installed.

    Used by infra-setup copy so auth/install guidance matches the user's
    environment instead of assuming `pipx`.
    """
    exe = Path(executable or sys.argv[0]).resolve()
    exe_str = str(exe)
    venv = os.environ.get("VIRTUAL_ENV")
    if "/pipx/venvs/" in exe_str or "/.local/pipx/venvs/" in exe_str:
        return "pipx"
    if "/uv/tools/" in exe_str or "/.local/share/uv/tools/" in exe_str:
        return "uv-tool"
    if venv and exe_str.startswith(str(Path(venv).resolve())):
        return "venv"
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return "venv"
    return "pip"
