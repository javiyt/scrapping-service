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
