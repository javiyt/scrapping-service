"""Plugin installation engine for SCRAPER_PLUGINS.

This module provides the core logic for installing plugins from environment
variables before the service starts. It handles:

- Parsing SCRAPER_PLUGINS into pip requirement specifiers.
- Computing a fingerprint to enable cache-skipping when requirements are unchanged.
- Invoking pip install --target with appropriate constraints and index options.
- Credential redaction in logs.
- Offline install support (wheelhouse).
- Fail/warn error handling.
- PYTHONPATH setup so plugins are discoverable via the loader.

All configuration comes from environment variables defined in the issue scope
(e.g., SCRAPER_PLUGINS, SCRAPER_PLUGINS_INDEX_URL, SCRAPER_PLUGINS_OFFLINE).

Usage::

    from app.plugins.install import install_plugins

    # Called early in app/run.py before exec
    result = install_plugins()
    if result.failed:
        # handle failure
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

#: Default plugins directory (configurable via SCRAPER_PLUGINS_DIR)
DEFAULT_PLUGINS_DIR = "/app/plugins"
#: Default constraints file path (configurable via SCRAPER_PLUGINS_CONSTRAINTS)
DEFAULT_CONSTRAINTS_FILE = "/app/requirements.txt"
#: Default wheelhouse directory for offline mode (configurable via SCRAPER_PLUGINS_WHEELHOUSE)
DEFAULT_WHEELHOUSE_DIR = "/plugins/wheels"
#: Fingerprint file name stored next to plugins dir
FINGERPRINT_FILE = ".install-fingerprint"
#: Environment variable prefix
ENV_PREFIX = "SCRAPER_PLUGINS_"

logger = logging.getLogger("scraper-api.plugins.install")


class InstallResult:
    """Result of an install operation."""

    def __init__(
        self,
        success: bool,
        skipped: bool = False,
        skip_reason: str | None = None,
        installed: list[str] | None = None,
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.skipped = skipped
        self.skip_reason = skip_reason
        self.installed = installed if installed is not None else []
        self.warnings = warnings if warnings is not None else []
        self.error = error

    def __repr__(self) -> str:
        return (
            f"InstallResult(success={self.success}, skipped={self.skipped}, "
            f"skip_reason={self.skip_reason}, installed={self.installed}, "
            f"warnings={self.warnings}, error={self.error})"
        )


def redact_credentials(url: str) -> str:
    """Redact user:pass credentials from a URL for safe logging.

    Example:
        "https://user:secret@example.com/repo" → "https://user:***@example.com/repo"
    """
    # Match scheme://user:pass@host
    pattern = r"((?:https?|ftp)://)([^:@/]+):([^@/]+)@([^/]+)(.*)"
    match = re.match(pattern, url)
    if match:
        scheme, user, _pass, host, rest = match.groups()
        return f"{scheme}{user}:***@{host}{rest}"
    return url


def parse_requirements(raw: str) -> list[str]:
    """Parse SCRAPER_PLUGINS value into a list of pip requirement specifiers.

    Accepts comma and/or space-separated entries. Trims whitespace, drops empties.
    Accepts:
        - PyPI package names and specs: pkg, pkg==1.0.0, pkg>=1.0,<2.0
        - VCS URLs: git+https://github.com/owner/repo@v1.0
        - Local wheel paths (absolute or relative to working dir)

    Returns a list of normalized strings.
    """
    if not raw:
        return []

    # Split on commas and whitespace
    # First replace commas with spaces, then split on whitespace
    parts = re.split(r"[,\s]+", raw.strip())
    parts = [p.strip() for p in parts if p.strip()]
    return parts


def read_requirements_file(filepath: str) -> list[str]:
    """Read and parse a requirements.txt-style file.

    Supports inline comments (# ...), blank lines, and direct URLs.
    """
    if not filepath:
        return []
    path = Path(filepath)
    if not path.is_file():
        logger.warning(f"SCRAPER_PLUGINS_REQUIREMENTS_FILE '{filepath}' not found; skipping")
        return []
    text = path.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments
        if "#" in line:
            line = line.split("#")[0].strip()
        if line:
            lines.append(line)
    return lines


def compute_fingerprint(specs: list[str], requirements_file: str | None) -> str:
    """Compute a SHA-256 fingerprint of the requirement set.

    Deterministic: sorts specs, includes requirements file content hash if set.
    Index URLs are NOT included in the fingerprint (credentials not logged).
    """
    # Normalize: sort specs lexicographically
    sorted_specs = sorted(specs)

    # Start with specs hash
    h = hashlib.sha256()
    for spec in sorted_specs:
        h.update(spec.encode("utf-8"))
        h.update(b"\n")

    # Include requirements file content if set
    if requirements_file:
        try:
            content = Path(requirements_file).read_bytes()
            h.update(content)
        except Exception:
            # If file disappears between write and read, don't fail; treat as changed
            h.update(b"<missing-file>")

    return h.hexdigest()


def build_pip_command(
    *,
    target_dir: str,
    constraints_file: str,
    specs: list[str],
    requirements_file: str | None,
    index_url: str | None,
    extra_index_url: str | None,
    offline: bool,
    wheelhouse_dir: str,
) -> list[str]:
    """Build the pip install command list.

    Args:
        target_dir: Plugin installation directory (required).
        constraints_file: Constraints file path (default DEFAULT_CONSTRAINTS_FILE).
        specs: List of requirement specifiers.
        requirements_file: Optional requirements file path.
        index_url: Optional primary index URL.
        extra_index_url: Optional extra index URL.
        offline: If True, use --no-index --find-links.
        wheelhouse_dir: Wheelhouse directory for offline mode.

    Returns:
        Command list suitable for subprocess.run(..., check=True).
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        target_dir,
    ]

    # Constraints
    if constraints_file:
        cmd.extend(["-c", constraints_file])

    # Index options
    if offline:
        cmd.extend(["--no-index", "--find-links", wheelhouse_dir])
    else:
        if index_url:
            cmd.extend(["--index-url", redact_credentials(index_url)])
        if extra_index_url:
            cmd.extend(["--extra-index-url", redact_credentials(extra_index_url)])

    # No cache (container should not persist cache; avoids permission issues)
    cmd.append("--no-cache-dir")

    # Add requirements file if provided
    if requirements_file:
        cmd.extend(["-r", requirements_file])

    # Then inline specs
    if specs:
        cmd.extend(specs)

    return cmd


def _parse_installed_names(pip_output: str) -> list[str]:
    """Parse pip install output to extract installed package names and versions.

    Attempts to match lines like:
        Successfully installed pkg1-1.0.0 pkg2-2.1.0
        Looking in indexes: ...
        Collecting pkg1 from ...
        ...

    Returns a list of "pkg==version" or "pkg @ url" strings.
    """
    lines = pip_output.splitlines()
    # Look for "Successfully installed ..."
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("Successfully installed "):
            # Strip prefix
            body = line[len("Successfully installed ") :]
            # Body is space-separated "pkg-ver" tokens, possibly with deps
            tokens = body.split()
            installed = []
            for tok in tokens:
                # Handle various formats:
                # pkg-1.0.0
                # pkg @ https://...
                if tok.startswith("https://") or tok.startswith("http://"):
                    # URL-style; use as-is
                    installed.append(tok)
                elif "-" in tok and not tok.startswith("-"):
                    # pkg-version
                    parts = tok.rsplit("-", 1)
                    if len(parts) == 2:
                        name, version = parts
                        installed.append(f"{name}=={version}")
                    else:
                        installed.append(tok)
                else:
                    installed.append(tok)
            return installed
    # Fallback: try to extract from "Requirement already satisfied" lines
    installed = []
    for line in lines:
        # Already satisfied lines look like:
        # Requirement already satisfied: pkg in /path (1.0.0)
        # or Requirement already satisfied: pkg from git+https://... in /path
        match = re.search(
            r"Requirement already satisfied:\s+(\S+)\s+in\s+(?:\S+)\s+\(([^)]+)\)",
            line,
        )
        if match:
            name = match.group(1)
            version = match.group(2)
            installed.append(f"{name}=={version}")
    return installed


def install_plugins() -> InstallResult:
    """Install plugins defined by environment variables.

    Reads SCRAPER_PLUGINS and related env vars, computes fingerprint,
    skips install if fingerprint matches, otherwise runs pip install.

    Returns:
        InstallResult with success status and metadata.
    """
    # Read env vars
    raw_specs = os.environ.get(f"{ENV_PREFIX}PLUGINS", "").strip()
    plugins_dir = os.environ.get(f"{ENV_PREFIX}DIR", DEFAULT_PLUGINS_DIR)
    constraints_file = os.environ.get(f"{ENV_PREFIX}CONSTRAINTS", DEFAULT_CONSTRAINTS_FILE)
    index_url = os.environ.get(f"{ENV_PREFIX}INDEX_URL")
    extra_index_url = os.environ.get(f"{ENV_PREFIX}EXTRA_INDEX_URL")
    requirements_file = os.environ.get(f"{ENV_PREFIX}REQUIREMENTS_FILE")
    offline = os.environ.get(f"{ENV_PREFIX}OFFLINE", "").strip().lower() in {"1", "true", "yes"}
    wheelhouse_dir = os.environ.get(f"{ENV_PREFIX}WHEELHOUSE", DEFAULT_WHEELHOUSE_DIR)
    on_error = os.environ.get(f"{ENV_PREFIX}ON_ERROR", "fail").strip().lower()

    # Parse specs
    specs = parse_requirements(raw_specs)

    # Read requirements file if set
    file_specs = read_requirements_file(requirements_file) if requirements_file else []

    # Combine and dedupe (file first, then inline)
    all_specs = file_specs + specs

    if not all_specs:
        logger.info("SCRAPER_PLUGINS empty; skipping plugin installation")
        return InstallResult(success=True, skipped=True, skip_reason="no requirements")

    # Compute fingerprint
    fingerprint = compute_fingerprint(all_specs, requirements_file)

    # Check cache
    plugins_path = Path(plugins_dir)
    fingerprint_file = plugins_path / FINGERPRINT_FILE

    if plugins_path.is_dir() and any(plugins_path.iterdir()):
        if fingerprint_file.is_file():
            stored = fingerprint_file.read_text(encoding="utf-8").strip()
            if stored == fingerprint:
                logger.info("Plugin cache hit (fingerprint unchanged); skipping installation")
                return InstallResult(success=True, skipped=True, skip_reason="cache hit")
        else:
            # Directory non-empty but no fingerprint — might be pre-existing plugins
            # Consider it a cache hit if the directory is non-empty (user has plugins)
            logger.info("Plugin directory non-empty but fingerprint missing; assuming cache hit")
            return InstallResult(success=True, skipped=True, skip_reason="directory exists")

    # Ensure plugins directory exists and is writable
    try:
        plugins_path.mkdir(parents=True, exist_ok=True)
        # Ensure it's writable (might be mounted)
        test_file = plugins_path / ".writable-test"
        test_file.touch()
        test_file.unlink()
    except Exception as exc:
        msg = f"Failed to create/write plugins directory '{plugins_dir}': {exc}"
        if on_error == "fail":
            logger.error(msg)
            return InstallResult(success=False, error=msg)
        else:
            logger.warning(f"{msg}; continuing without plugins")
            return InstallResult(success=True, warnings=[msg])

    # Build command
    cmd = build_pip_command(
        target_dir=plugins_dir,
        constraints_file=constraints_file,
        specs=all_specs,
        requirements_file=requirements_file,
        index_url=index_url,
        extra_index_url=extra_index_url,
        offline=offline,
        wheelhouse_dir=wheelhouse_dir,
    )

    # Log start (redact URLs)
    logger.info(
        "Installing plugins (offline=%s, constraints=%s, index_url=%s, extra_index_url=%s)",
        offline,
        constraints_file,
        redact_credentials(index_url) if index_url else "none",
        redact_credentials(extra_index_url) if extra_index_url else "none",
    )
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": plugins_dir},
        )
        elapsed = time.time() - start

        if result.returncode != 0:
            # Parse error
            err = result.stderr.strip() or result.stdout.strip()
            err = redact_credentials(err)
            msg = f"Plugin installation failed: {err}"
            if on_error == "fail":
                logger.error(
                    "%s (elapsed %.2fs); exiting due to SCRAPER_PLUGINS_ON_ERROR=fail",
                    msg,
                    elapsed,
                )
                return InstallResult(success=False, error=msg)
            else:
                logger.warning(
                    "%s (elapsed %.2fs); continuing due to SCRAPER_PLUGINS_ON_ERROR=warn",
                    msg,
                    elapsed,
                )
                return InstallResult(success=True, warnings=[msg])

        # Success
        output = result.stdout
        installed = _parse_installed_names(output)
        elapsed_str = f"{elapsed:.2f}s"

        logger.info(
            "Plugins installed successfully in %s. Installed: %s",
            elapsed_str,
            ", ".join(installed) if installed else "(no new packages)",
        )

        # Write fingerprint
        fingerprint_file.write_text(fingerprint, encoding="utf-8")

        return InstallResult(
            success=True,
            installed=installed,
        )

    except subprocess.CalledProcessError as exc:
        elapsed = time.time() - start
        msg = f"Plugin installation subprocess error: {exc}"
        if on_error == "fail":
            logger.error("%s (elapsed %.2fs); exiting", msg, elapsed)
            return InstallResult(success=False, error=msg)
        else:
            logger.warning("%s (elapsed %.2fs); continuing", msg, elapsed)
            return InstallResult(success=True, warnings=[msg])
    except Exception as exc:
        elapsed = time.time() - start
        msg = f"Plugin installation exception: {exc}"
        if on_error == "fail":
            logger.error("%s (elapsed %.2fs); exiting", msg, elapsed)
            return InstallResult(success=False, error=msg)
        else:
            logger.warning("%s (elapsed %.2fs); continuing", msg, elapsed)
            return InstallResult(success=True, warnings=[msg])
