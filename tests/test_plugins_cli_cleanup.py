"""Tests for plugin CLI cleanup functionality."""

from __future__ import annotations

import os
from unittest import mock

from app.plugins.cli import _cleanup_constraints_file


class TestCleanupConstraintsFile:
    """Test temp constraints file cleanup."""

    def test_cleanup_constraints_file_removes_existing(self, tmp_path: str) -> None:
        """Cleanup removes existing constraints file."""
        constraints_file = os.path.join(tmp_path, "constraints-abc123.txt")
        # Create file
        with open(constraints_file, "w") as f:
            f.write("fastapi>=0.162.0")

        assert os.path.exists(constraints_file)

        _cleanup_constraints_file(constraints_file)

        assert not os.path.exists(constraints_file)

    def test_cleanup_constraints_file_noop_on_missing(self) -> None:
        """Cleanup does nothing when file doesn't exist."""
        # Should not raise
        _cleanup_constraints_file("/nonexistent/path/constraints.txt")

    def test_cleanup_constraints_file_handles_none(self) -> None:
        """Cleanup handles None input gracefully."""
        # Should not raise
        _cleanup_constraints_file(None)

    def test_cleanup_constraints_file_preserves_on_error(self) -> None:
        """Cleanup prints warning but doesn't fail on OS error."""
        with mock.patch("os.unlink", side_effect=OSError("Permission denied")):
            # Should not raise (best-effort)
            _cleanup_constraints_file("/some/file.txt")
