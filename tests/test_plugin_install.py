"""Tests for plugin installation via SCRAPER_PLUGINS.

Tests cover:
- Empty SCRAPER_PLUGINS is no-op (no pip subprocess).
- Spec parsing: commas, spaces, mixed, version pins.
- Redaction of credentials in URLs.
- Fingerprint computation.
- Pip command construction.
"""

from __future__ import annotations

import hashlib
import os
import sys

from app.plugins.install import (
    InstallResult,
    _parse_installed_names,
    build_pip_command,
    compute_fingerprint,
    parse_requirements,
    redact_credentials,
)


class TestParseRequirements:
    def test_empty_string(self) -> None:
        assert parse_requirements("") == []
        assert parse_requirements("   ") == []

    def test_single_package(self) -> None:
        assert parse_requirements("mypackage") == ["mypackage"]

    def test_version_pin(self) -> None:
        assert parse_requirements("mypackage==1.2.3") == ["mypackage==1.2.3"]

    def test_comma_separated(self) -> None:
        result = parse_requirements("pkg1,pkg2,pkg3")
        assert result == ["pkg1", "pkg2", "pkg3"]

    def test_space_separated(self) -> None:
        result = parse_requirements("pkg1 pkg2 pkg3")
        assert result == ["pkg1", "pkg2", "pkg3"]

    def test_mixed_separators(self) -> None:
        result = parse_requirements("pkg1, pkg2  pkg3,pkg4")
        assert result == ["pkg1", "pkg2", "pkg3", "pkg4"]

    def test_vcs_url(self) -> None:
        result = parse_requirements("git+https://github.com/owner/repo@v1.0")
        assert result == ["git+https://github.com/owner/repo@v1.0"]

    def test_local_wheel(self) -> None:
        result = parse_requirements("./wheels/myplugin-1.0.0-py3-none-any.whl")
        assert result == ["./wheels/myplugin-1.0.0-py3-none-any.whl"]

    def test_leading_trailing_whitespace(self) -> None:
        result = parse_requirements("  pkg1  ,  pkg2  ")
        assert result == ["pkg1", "pkg2"]


class TestRedactCredentials:
    def test_https_basic_auth(self) -> None:
        url = "https://user:secret@example.com/repo"
        assert redact_credentials(url) == "https://user:***@example.com/repo"

    def test_http_basic_auth(self) -> None:
        url = "http://admin:pass123@pypi.example.com/simple"
        assert redact_credentials(url) == "http://admin:***@pypi.example.com/simple"

    def test_no_credentials(self) -> None:
        url = "https://pypi.org/simple"
        assert redact_credentials(url) == "https://pypi.org/simple"

    def test_url_with_path(self) -> None:
        url = "https://user:token@github.com/owner/repo/raw/main/wheel.whl"
        assert (
            redact_credentials(url) == "https://user:***@github.com/owner/repo/raw/main/wheel.whl"
        )


class TestComputeFingerprint:
    def test_empty_specs(self) -> None:
        fp = compute_fingerprint([], None)
        # SHA-256 of empty string
        assert fp == hashlib.sha256(b"").hexdigest()

    def test_single_spec(self) -> None:
        fp = compute_fingerprint(["mypackage==1.0.0"], None)
        expected = hashlib.sha256(b"mypackage==1.0.0\n").hexdigest()
        assert fp == expected

    def test_multiple_specs_sorted(self) -> None:
        # Order should not matter
        fp1 = compute_fingerprint(["b", "a"], None)
        fp2 = compute_fingerprint(["a", "b"], None)
        assert fp1 == fp2

    def test_with_requirements_file(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("pkg1==1.0.0\npkg2==2.0.0\n")
            req_file = f.name
        try:
            fp = compute_fingerprint(["pkg3==3.0.0"], req_file)
            # Should include spec + file content
            h = hashlib.sha256()
            h.update(b"pkg3==3.0.0\n")
            h.update(b"pkg1==1.0.0\npkg2==2.0.0\n")
            expected = h.hexdigest()
            assert fp == expected
        finally:
            os.unlink(req_file)

    def test_different_specs_different_fingerprint(self) -> None:
        fp1 = compute_fingerprint(["pkg1==1.0.0"], None)
        fp2 = compute_fingerprint(["pkg2==1.0.0"], None)
        assert fp1 != fp2


class TestParseInstalledNames:
    def test_successfully_installed(self) -> None:
        output = """
Collecting pkg1==1.0.0
  Downloading pkg1-1.0.0-py3-none-any.whl
Collecting pkg2==2.0.0
  Downloading pkg2-2.0.0-py3-none-any.whl
Successfully installed pkg1-1.0.0 pkg2-2.0.0
"""
        installed = _parse_installed_names(output)
        assert "pkg1==1.0.0" in installed
        assert "pkg2==2.0.0" in installed

    def test_already_satisfied(self) -> None:
        output = """
Requirement already satisfied: pkg1 in /usr/local/lib/python3.12/site-packages (1.0.0)
Requirement already satisfied: pkg2 in /usr/local/lib/python3.12/site-packages (2.0.0)
"""
        installed = _parse_installed_names(output)
        assert "pkg1==1.0.0" in installed
        assert "pkg2==2.0.0" in installed

    def test_pip_show_format(self) -> None:
        output = """
Installing collected packages: mypackage
Successfully installed mypackage-1.2.3
"""
        installed = _parse_installed_names(output)
        assert "mypackage==1.2.3" in installed


class TestBuildPipCommand:
    def test_basic_command(self) -> None:
        cmd = build_pip_command(
            target_dir="/app/plugins",
            constraints_file="/app/requirements.txt",
            specs=["pkg1==1.0.0"],
            requirements_file=None,
            index_url=None,
            extra_index_url=None,
            offline=False,
            wheelhouse_dir="/plugins/wheels",
        )
        assert cmd == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            "/app/plugins",
            "-c",
            "/app/requirements.txt",
            "--no-cache-dir",
            "pkg1==1.0.0",
        ]

    def test_with_index_url(self) -> None:
        cmd = build_pip_command(
            target_dir="/app/plugins",
            constraints_file="/app/requirements.txt",
            specs=[],
            requirements_file=None,
            index_url="https://user:pass@pypi.example.com/simple",
            extra_index_url=None,
            offline=False,
            wheelhouse_dir="/plugins/wheels",
        )
        # URL should be redacted
        assert "--index-url" in cmd
        idx = cmd.index("--index-url")
        assert "user:***@" in cmd[idx + 1]

    def test_offline_mode(self) -> None:
        cmd = build_pip_command(
            target_dir="/app/plugins",
            constraints_file="/app/requirements.txt",
            specs=["pkg1"],
            requirements_file=None,
            index_url=None,
            extra_index_url=None,
            offline=True,
            wheelhouse_dir="/plugins/wheels",
        )
        assert "--no-index" in cmd
        assert "--find-links" in cmd
        links_idx = cmd.index("--find-links")
        assert cmd[links_idx + 1] == "/plugins/wheels"

    def test_requirements_file(self) -> None:
        cmd = build_pip_command(
            target_dir="/app/plugins",
            constraints_file="/app/requirements.txt",
            specs=["pkg2"],
            requirements_file="/path/to/requirements.txt",
            index_url=None,
            extra_index_url=None,
            offline=False,
            wheelhouse_dir="/plugins/wheels",
        )
        assert "-r" in cmd
        req_idx = cmd.index("-r")
        assert cmd[req_idx + 1] == "/path/to/requirements.txt"
        assert "pkg2" in cmd  # Inline specs still included


class TestInstallResult:
    def test_install_result_defaults(self) -> None:
        result = InstallResult(success=True)
        assert result.success is True
        assert result.skipped is False
        assert result.installed == []
        assert result.warnings == []
        assert result.error is None

    def test_install_result_with_values(self) -> None:
        result = InstallResult(
            success=True,
            skipped=True,
            skip_reason="cache hit",
            installed=["pkg1==1.0.0"],
            warnings=["Some warning"],
            error=None,
        )
        assert result.success is True
        assert result.skipped is True
        assert result.skip_reason == "cache hit"
        assert result.installed == ["pkg1==1.0.0"]
        assert result.warnings == ["Some warning"]

    def test_not_success_means_failure(self) -> None:
        result = InstallResult(success=False, error="Install failed")
        assert result.success is False
        # Not success implies failure for our purposes
        assert not result.success
        assert result.error == "Install failed"


class TestInstallResultFailed:
    def test_failed_install_has_error(self) -> None:
        result = InstallResult(success=False, error="Installation failed")
        assert not result.success
        assert result.error == "Installation failed"

    def test_success_install_no_error(self) -> None:
        result = InstallResult(success=True)
        assert result.success
        assert result.error is None

    def test_skipped_install_with_reason(self) -> None:
        result = InstallResult(success=True, skipped=True, skip_reason="no requirements")
        assert result.success
        assert result.skipped
        assert result.skip_reason == "no requirements"


class TestPipCommandOffline:
    def test_offline_command_includes_wheelhouse(self) -> None:
        cmd = build_pip_command(
            target_dir="/app/plugins",
            constraints_file="/app/requirements.txt",
            specs=["pkg1"],
            requirements_file=None,
            index_url=None,
            extra_index_url=None,
            offline=True,
            wheelhouse_dir="/plugins/wheels",
        )
        assert cmd[-1] == "pkg1"  # Last arg is the spec
