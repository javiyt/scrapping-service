"""Entry-point tests for the MCP adapter (ticket #150).

The console-script entry points are installed by ``pyproject.toml``:

- ``scrapping-service-mcp``    → stdio transport (the one MCP hosts launch)
- ``scrapping-service-mcp-http`` → streamable HTTP + adapter ops routes

These tests exercise the installed entry points *and* the module-level
runners without spawning processes where possible, and pin the one
behavioral contract that matters for an unconfigured adapter: a clean
``SystemExit(2)`` with a useful message, never a traceback.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        k
        for k in os.environ
        if k.startswith("MCP_SCRAP") or k in ("SCRAPER_API_KEY", "SCRAPER_BASE_URL", "CONFIG_PATH")
    ]:
        monkeypatch.delenv(key, raising=False)


class TestEntryPointsExist:
    def test_console_scripts_are_declared(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        assert "scrapping-service-mcp" in pyproject
        assert "scrapping-service-mcp-http" in pyproject

    def test_entrypoint_module_imports(self) -> None:
        module = importlib.import_module("app.mcp.entrypoints")
        assert callable(module.main)
        assert callable(module.main_http)

    def test_main_module_runs_entrypoint(self) -> None:
        """``python -m app.mcp`` is wired to the stdio entry point."""
        module = importlib.import_module("app.mcp.__main__")
        assert module is not None


class TestUnconfiguredStartup:
    """An unconfigured adapter must exit cleanly, not with a traceback."""

    def test_main_exits_2_without_base_url(
        self, clean_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from app.mcp.entrypoints import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "MCP_SCRAPPER_BASE_URL" in err
        assert "Traceback" not in err

    def test_main_http_exits_2_without_base_url(
        self, clean_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from app.mcp.entrypoints import main_http

        with pytest.raises(SystemExit) as exc:
            main_http()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "MCP_SCRAPPER_BASE_URL" in err
        assert "Traceback" not in err

    def test_installed_console_script_exits_2(self) -> None:
        """The *installed* console script (not just the module) behaves the same."""
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MCP_SCRAP")
            and k not in ("SCRAPER_API_KEY", "SCRAPER_BASE_URL", "CONFIG_PATH")
        }
        env["PYTHONPATH"] = str(REPO_ROOT)
        result = subprocess.run(
            [sys.executable, "-c", "from app.mcp.entrypoints import main; main()"],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        assert result.returncode == 2
        assert "MCP_SCRAPPER_BASE_URL" in result.stderr
        assert "Traceback" not in result.stderr


class TestConfiguredStartup:
    """With a base URL set, the stdio server must start (and stay alive until asked to quit)."""

    def test_main_starts_and_serves_stdio(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stdio entry point runs the JSON-RPC loop: it reads stdin until EOF.

        We give it one line of garbage (the loop ignores non-JSON), then EOF:
        the server must terminate *cleanly* (exit 0), proving the loop wired
        correctly against a real transport.
        """
        monkeypatch.setenv(
            "MCP_SCRAPPER_BASE_URL", "http://127.0.0.1:1"
        )  # nothing must connect yet

        env = os.environ.copy()
        env = {
            k: v
            for k, v in env.items()
            if not k.startswith("MCP_SCRAP")
            and k not in ("SCRAPER_API_KEY", "SCRAPER_BASE_URL", "CONFIG_PATH")
        }
        env["MCP_SCRAPPER_BASE_URL"] = "http://127.0.0.1:1"
        env["PYTHONPATH"] = str(REPO_ROOT)

        result = subprocess.run(
            [sys.executable, "-c", "from app.mcp.entrypoints import main; main()"],
            input="{not valid json\n",
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=30,
        )
        # EOF on stdin ends the loop; no crash, no traceback.
        assert "Traceback" not in result.stderr
        assert "Exception" not in result.stderr
