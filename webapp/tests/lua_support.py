"""Run synthetic Lua programs only after opting into the local DCS evaluator."""
import os
from pathlib import Path
import subprocess

import pytest


def run_lua(tmp_path, program):
    if os.environ.get("DCS_LOCAL_INTEGRATION") != "1":
        pytest.skip("Standalone DCS Lua evaluator: set DCS_LOCAL_INTEGRATION=1 to opt in")
    configured = os.environ.get("DCS_TEST_LUA")
    if configured:
        lua = Path(configured)
    else:
        from webapp.backend.discovery import SetupUnavailable, resolve_setup_paths
        try:
            install, _ = resolve_setup_paths()
        except SetupUnavailable:
            pytest.skip("No unique installed DCS; set DCS_TEST_LUA to a standalone luae.exe")
        lua = Path(install) / "bin" / "luae.exe"
    if not lua.is_file():
        pytest.skip("Standalone Lua evaluator unavailable; set DCS_TEST_LUA to luae.exe")
    path = tmp_path / "synthetic-test.lua"
    path.write_text(program, encoding="utf-8")
    result = subprocess.run([str(lua), str(path)], capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout
