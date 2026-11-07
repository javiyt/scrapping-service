"""Tests for the plugin CLI (app.plugins.__main__).

These tests verify the `list` and `check` commands and error handling.
"""

from unittest.mock import patch

from app.plugins import __main__ as cli


def test_cli_list_with_no_plugins():
    """CLI list command with no plugins should print 'No plugins discovered'."""
    mock_result = type(
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

    with patch("app.plugins.__main__.load_plugins", return_value=mock_result):
        with patch("app.plugins.__main__.print") as mock_print:
            ret = cli.cmd_list(None)
            assert ret == 0
            mock_print.assert_any_call("No plugins discovered")


def test_cli_list_with_one_plugin():
    """CLI list command with plugins should print metadata."""
    mock_result = type(
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

    with patch("app.plugins.__main__.load_plugins", return_value=mock_result):
        with patch("app.plugins.__main__.print") as mock_print:
            ret = cli.cmd_list(None)
            assert ret == 0
            calls = [str(call) for call in mock_print.call_args_list]
            assert any("sample-plugin v1.2.3" in str(c) for c in calls)


def test_cli_check_with_no_failures():
    """CLI check command with no failures should exit 0."""
    mock_result = type(
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

    with patch("app.plugins.__main__.load_plugins", return_value=mock_result):
        with patch("app.plugins.__main__.print") as mock_print:
            ret = cli.cmd_check(None)
            assert ret == 0
            mock_print.assert_any_call("All plugins loaded successfully")


def test_cli_check_with_failures():
    """CLI check command with failures should exit 1."""
    mock_result = type(
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

    with patch("app.plugins.__main__.load_plugins", return_value=mock_result):
        with patch("app.plugins.__main__.print") as mock_print:
            ret = cli.cmd_check(None)
            assert ret == 1
            calls = [str(call) for call in mock_print.call_args_list]
            assert any("FAILED plugins:" in str(c) for c in calls)


def test_cli_main_without_args():
    """CLI main without args should print usage and return 1."""
    ret = cli.main([])
    assert ret == 1


def test_cli_main_unknown_command():
    """CLI main with unknown command should print error and return 1."""
    ret = cli.main(["unknown"])
    assert ret == 1


def test_cli_main_list_command():
    """CLI main list command should call cmd_list."""
    with patch("app.plugins.__main__.cmd_list", return_value=0) as mock_cmd:
        ret = cli.main(["list"])
        assert ret == 0
        mock_cmd.assert_called_once()


def test_cli_main_check_command():
    """CLI main check command should call cmd_check."""
    with patch("app.plugins.__main__.cmd_check", return_value=0) as mock_cmd:
        ret = cli.main(["check"])
        assert ret == 0
        mock_cmd.assert_called_once()
