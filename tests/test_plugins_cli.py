"""Tests for the plugin CLI (app.plugins.cli).

These tests verify the `list`, `check`, `install`, `uninstall` commands and error handling.
"""

from unittest.mock import patch

from app.plugins import cli


def test_cli_list_with_no_plugins():
    """CLI list command with no plugins should print 'No plugins discovered'."""
    # Mock load_plugins and print inside cmd_list
    with patch("app.plugins.cli.load_plugins") as mock_load:
        mock_load.return_value = type(
            "MockResult",
            (),
            {
                "metadata": [],
                "loaded": [],
                "skipped": [],
                "failed": [],
                "enabled": [],
                "on_error": "fail",
            },
        )()
        with patch("app.plugins.cli.print") as mock_print:
            ret = cli.cmd_list()
            assert ret == 0
            mock_print.assert_any_call("No plugins discovered")


def test_cli_list_with_one_plugin():
    """CLI list command with plugins should print metadata."""
    with patch("app.plugins.cli.load_plugins") as mock_load:
        mock_load.return_value = type(
            "MockResult",
            (),
            {
                "metadata": [
                    {
                        "name": "sample-plugin",
                        "version": "1.2.3",
                        "api_version": "1.0.0",
                        "service_api_version": "1.0.0",
                        "status": "loaded",
                        "reason": None,
                        "extensions": {
                            "search_providers": ["sample-search"],
                            "transcript_providers": [],
                            "browser_provider": None,
                            "extractors": [],
                        },
                    }
                ],
                "loaded": ["sample-plugin"],
                "skipped": [],
                "failed": [],
                "enabled": ["sample-plugin"],
                "on_error": "fail",
            },
        )()
        with patch("app.plugins.cli.print") as mock_print:
            ret = cli.cmd_list()
            assert ret == 0
            calls = [str(call) for call in mock_print.call_args_list]
            assert any("sample-plugin v1.2.3" in str(c) for c in calls)


def test_cli_check_with_no_failures():
    """CLI check command with no failures should exit 0."""
    with patch("app.plugins.cli.load_plugins") as mock_load:
        mock_load.return_value = type(
            "MockResult",
            (),
            {
                "metadata": [
                    {
                        "name": "good-plugin",
                        "version": "1.0.0",
                        "api_version": "1.0.0",
                        "service_api_version": "1.0.0",
                        "status": "loaded",
                        "reason": None,
                        "extensions": {
                            "search_providers": [],
                            "transcript_providers": [],
                            "browser_provider": None,
                            "extractors": [],
                        },
                    }
                ],
                "loaded": ["good-plugin"],
                "skipped": [],
                "failed": [],
                "enabled": ["good-plugin"],
                "on_error": "fail",
            },
        )()
        with patch("app.plugins.cli.print") as mock_print:
            ret = cli.cmd_check()
            assert ret == 0
            mock_print.assert_any_call("All plugins loaded successfully")


def test_cli_check_with_failures():
    """CLI check command with failures should exit 1."""
    with patch("app.plugins.cli.load_plugins") as mock_load:
        mock_load.return_value = type(
            "MockResult",
            (),
            {
                "metadata": [
                    {
                        "name": "broken-plugin",
                        "version": "unknown",
                        "api_version": "unknown",
                        "service_api_version": "1.0.0",
                        "status": "failed",
                        "reason": "ImportError: no module named 'x'",
                        "extensions": {
                            "search_providers": [],
                            "transcript_providers": [],
                            "browser_provider": None,
                            "extractors": [],
                        },
                    }
                ],
                "loaded": [],
                "skipped": [],
                "failed": [{"name": "broken-plugin", "reason": "ImportError: no module named 'x'"}],
                "enabled": [],
                "on_error": "fail",
            },
        )()
        with patch("app.plugins.cli.print") as mock_print:
            ret = cli.cmd_check()
            assert ret == 1
            calls = [str(call) for call in mock_print.call_args_list]
            assert any("FAILED plugins:" in str(c) for c in calls)


def test_cli_run_plugins_command_without_args():
    """CLI dispatch without args should print usage and return 0 (help)."""
    ret = cli.run_plugins_command([])
    # cmd_help prints help and returns 0
    assert ret == 0


def test_cli_run_plugins_command_unknown_command():
    """CLI dispatch with unknown command should print error and return 1."""
    ret = cli.run_plugins_command(["unknown"])
    assert ret == 1


def test_cli_run_plugins_command_list():
    """CLI dispatch list command should call cmd_list."""
    with patch("app.plugins.cli.cmd_list", return_value=0) as mock_cmd:
        ret = cli.run_plugins_command(["list"])
        assert ret == 0
        mock_cmd.assert_called_once()


def test_cli_run_plugins_command_check():
    """CLI dispatch check command should call cmd_check."""
    with patch("app.plugins.cli.cmd_check", return_value=0) as mock_cmd:
        ret = cli.run_plugins_command(["check"])
        assert ret == 0
        mock_cmd.assert_called_once()


def test_cli_install_command():
    """CLI install command should build pip install cmd."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ret = cli.cmd_install(["mypackage==1.0.0"], constraints_file=None)
        assert ret == 0
        mock_run.assert_called()


def test_cli_uninstall_command():
    """CLI uninstall command should build pip uninstall cmd."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ret = cli.cmd_uninstall(["mypackage"], assume_yes=True)
        assert ret == 0
        mock_run.assert_called()


def test_cli_help_command():
    """CLI help command should return 0 and print help."""
    ret = cli.cmd_help()
    assert ret == 0


def test_cmd_install_with_constraints():
    """CLI install with constraints should pass constraints to pip."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ret = cli.cmd_install(["pkg==1.0"], constraints_file="/app/requirements.txt")
        assert ret == 0
        # Verify constraints file is passed
        cmd_args = mock_run.call_args[0][0]
        assert "-c" in cmd_args
        assert "/app/requirements.txt" in cmd_args


def test_cmd_install_with_index_url():
    """CLI install with index URL should redact credentials."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ret = cli.cmd_install(
            ["pkg==1.0"],
            constraints_file=None,
            index_url="https://user:pass@example.com/simple",
        )
        assert ret == 0
        cmd_args = mock_run.call_args[0][0]
        assert "--index-url" in cmd_args
        idx = cmd_args.index("--index-url")
        assert "user:***@" in cmd_args[idx + 1]


def test_cmd_install_offline():
    """CLI install offline should use --no-index and --find-links."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ret = cli.cmd_install(
            ["pkg==1.0"],
            constraints_file=None,
            offline=True,
            wheelhouse_dir="/wheels",
        )
        assert ret == 0
        cmd_args = mock_run.call_args[0][0]
        assert "--no-index" in cmd_args
        assert "--find-links" in cmd_args
        idx = cmd_args.index("--find-links")
        assert cmd_args[idx + 1] == "/wheels"


def test_cmd_uninstall_without_yes_flag():
    """CLI uninstall without --yes should prompt for confirmation."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type(
            "Result", (), {"returncode": 0, "stdout": "Would uninstall pkg", "stderr": ""}
        )()
        with patch("app.plugins.cli.input", return_value="n"):
            ret = cli.cmd_uninstall(["mypackage"], assume_yes=False)
            assert ret == 0  # Cancelled
            # dry-run is called, but not the actual uninstall
            calls = mock_run.call_args_list
            assert any("--dry-run" in str(c) for c in calls)
            assert not any("uninstall" in str(c) and "--dry-run" not in str(c) for c in calls)


def test_cmd_check_disabled_plugin():
    """CLI check with disabled plugin should print enable hint."""
    with patch("app.plugins.cli.load_plugins") as mock_load:
        mock_load.return_value = type(
            "MockResult",
            (),
            {
                "metadata": [
                    {
                        "name": "disabled-plugin",
                        "version": "1.0.0",
                        "api_version": "1.0.0",
                        "service_api_version": "1.0.0",
                        "status": "disabled",
                        "reason": "not enabled (opt-in allowlist)",
                        "extensions": {},
                    }
                ],
                "loaded": [],
                "skipped": [],
                "failed": [],
                "enabled": [],
                "on_error": "fail",
            },
        )()
        with patch("app.plugins.cli.print") as mock_print:
            ret = cli.cmd_check()
            assert ret == 1
            calls = [str(call) for call in mock_print.call_args_list]
            assert any("SCRAPER_PLUGINS_ENABLED" in str(c) for c in calls)


def test_cmd_install_error():
    """CLI install with pip failure should return non-zero."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "Error installing pkg"}
        )()
        ret = cli.cmd_install(["broken-pkg==1.0"], constraints_file=None)
        assert ret == 1


def test_cmd_uninstall_error():
    """CLI uninstall with pip failure should return non-zero."""
    with patch("app.plugins.cli._run_pip") as mock_run:
        mock_run.return_value = type(
            "Result", (), {"returncode": 1, "stdout": "", "stderr": "Error uninstalling pkg"}
        )()
        ret = cli.cmd_uninstall(["broken-pkg"], assume_yes=True)
        assert ret == 1
